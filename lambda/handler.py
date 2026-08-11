import json
import logging
import os
from datetime import datetime, timezone

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"
SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"

secrets_client = boto3.client("secretsmanager")
ses_client = boto3.client("ses")
ssm_client = boto3.client("ssm")


def get_google_credentials():
    secret_arn = os.environ["SECRET_ARN"]
    resp = secrets_client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def get_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def c_to_f(celsius):
    return celsius * 9 / 5 + 32


def get_device_traits(access_token, project_id, device_id):
    url = f"{SDM_BASE}/enterprises/{project_id}/devices/{device_id}"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10
    )
    resp.raise_for_status()
    traits = resp.json().get("traits", {})

    ambient_temp_f = c_to_f(
        traits["sdm.devices.traits.Temperature"]["ambientTemperatureCelsius"]
    )

    setpoint_trait = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
    cool_setpoint_c = setpoint_trait.get("coolCelsius")
    cool_setpoint_f = c_to_f(cool_setpoint_c) if cool_setpoint_c is not None else None

    return ambient_temp_f, cool_setpoint_f


def _parse_streak(streak):
    over_since = datetime.fromisoformat(streak["over_since"]) if streak.get("over_since") else None
    last_alert_at = (
        datetime.fromisoformat(streak["last_alert_at"]) if streak.get("last_alert_at") else None
    )
    return over_since, last_alert_at


def _serialize_streak(over_since, last_alert_at):
    return {
        "over_since": over_since.isoformat() if over_since else None,
        "last_alert_at": last_alert_at.isoformat() if last_alert_at else None,
    }


def get_state(param_name):
    value = ssm_client.get_parameter(Name=param_name)["Parameter"]["Value"]
    data = json.loads(value)
    return _parse_streak(data["ambient"]), _parse_streak(data["setpoint"])


def set_state(param_name, ambient_streak, setpoint_streak):
    data = {
        "ambient": _serialize_streak(*ambient_streak),
        "setpoint": _serialize_streak(*setpoint_streak),
    }
    ssm_client.put_parameter(Name=param_name, Value=json.dumps(data), Overwrite=True)


def evaluate_streak(is_over, now, over_since, last_alert_at, sustained_minutes_required, repeat_alert_minutes):
    """Returns (should_alert, new_over_since, new_last_alert_at, sustained_minutes)."""
    if not is_over:
        return False, None, None, 0.0

    if over_since is None:
        over_since = now
    sustained_minutes = (now - over_since).total_seconds() / 60

    should_alert = False
    if sustained_minutes >= sustained_minutes_required:
        if last_alert_at is None:
            should_alert = True
        else:
            minutes_since_last_alert = (now - last_alert_at).total_seconds() / 60
            should_alert = minutes_since_last_alert >= repeat_alert_minutes
        if should_alert:
            last_alert_at = now

    return should_alert, over_since, last_alert_at, sustained_minutes


def send_alert_email(sender, recipient, subject, body):
    ses_client.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )


def handler(event, context):
    threshold_f = float(os.environ.get("TEMP_THRESHOLD_F", "76"))
    sustained_minutes_required = float(os.environ.get("SUSTAINED_MINUTES", "30"))
    repeat_alert_minutes = float(os.environ.get("REPEAT_ALERT_MINUTES", "60"))
    setpoint_threshold_f = float(os.environ.get("SETPOINT_THRESHOLD_F", "76"))
    project_id = os.environ["NEST_PROJECT_ID"]
    device_id = os.environ["NEST_DEVICE_ID"]
    sender = os.environ["SENDER_EMAIL"]
    recipient = os.environ["RECIPIENT_EMAIL"]
    state_param_name = os.environ["STATE_PARAM_NAME"]

    creds = get_google_credentials()
    access_token = get_access_token(
        creds["google_client_id"],
        creds["google_client_secret"],
        creds["google_refresh_token"],
    )

    ambient_temp_f, setpoint_f = get_device_traits(access_token, project_id, device_id)
    now = datetime.now(timezone.utc)
    (ambient_over_since, ambient_last_alert_at), (setpoint_over_since, setpoint_last_alert_at) = get_state(
        state_param_name
    )

    logger.info("Ambient temperature: %.1fF (threshold %.0fF)", ambient_temp_f, threshold_f)
    if setpoint_f is not None:
        logger.info("Cooling setpoint: %.1fF (threshold %.0fF)", setpoint_f, setpoint_threshold_f)
    else:
        logger.info("No cooling setpoint reported (thermostat not in COOL/HEATCOOL mode).")

    ambient_alert_sent, ambient_over_since, ambient_last_alert_at, sustained_minutes = evaluate_streak(
        ambient_temp_f > threshold_f,
        now,
        ambient_over_since,
        ambient_last_alert_at,
        sustained_minutes_required,
        repeat_alert_minutes,
    )
    if ambient_alert_sent:
        send_alert_email(
            sender,
            recipient,
            subject=f"Thermostat alert: {ambient_temp_f:.1f}F (over {threshold_f:.0f}F)",
            body=(
                f"Your Nest thermostat is reading {ambient_temp_f:.1f}F, above your "
                f"{threshold_f:.0f}F threshold for {sustained_minutes:.0f} minutes, "
                f"as of {now.isoformat()}.\n\nTake a look."
            ),
        )
        logger.info("Ambient temperature alert email sent to %s", recipient)

    setpoint_alert_sent = False
    if setpoint_f is not None:
        setpoint_alert_sent, setpoint_over_since, setpoint_last_alert_at, _ = evaluate_streak(
            setpoint_f > setpoint_threshold_f,
            now,
            setpoint_over_since,
            setpoint_last_alert_at,
            0.0,
            repeat_alert_minutes,
        )
        if setpoint_alert_sent:
            send_alert_email(
                sender,
                recipient,
                subject=f"Thermostat alert: cooling setpoint {setpoint_f:.1f}F (over {setpoint_threshold_f:.0f}F)",
                body=(
                    f"Your Nest thermostat's cooling setpoint is now {setpoint_f:.1f}F, "
                    f"above your {setpoint_threshold_f:.0f}F threshold, as of "
                    f"{now.isoformat()}. Someone may have changed it.\n\nTake a look."
                ),
            )
            logger.info("Setpoint alert email sent to %s", recipient)

    set_state(
        state_param_name,
        (ambient_over_since, ambient_last_alert_at),
        (setpoint_over_since, setpoint_last_alert_at),
    )

    return {
        "ambient_temp_f": round(ambient_temp_f, 1),
        "sustained_minutes": round(sustained_minutes, 1),
        "ambient_alert_sent": ambient_alert_sent,
        "setpoint_f": round(setpoint_f, 1) if setpoint_f is not None else None,
        "setpoint_alert_sent": setpoint_alert_sent,
    }
