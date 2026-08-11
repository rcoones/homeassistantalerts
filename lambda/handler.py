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


def get_ambient_temp_f(access_token, project_id, device_id):
    url = f"{SDM_BASE}/enterprises/{project_id}/devices/{device_id}"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10
    )
    resp.raise_for_status()
    traits = resp.json().get("traits", {})
    temp_c = traits["sdm.devices.traits.Temperature"]["ambientTemperatureCelsius"]
    return temp_c * 9 / 5 + 32


def get_state(param_name):
    value = ssm_client.get_parameter(Name=param_name)["Parameter"]["Value"]
    data = json.loads(value)
    over_since = datetime.fromisoformat(data["over_since"]) if data.get("over_since") else None
    last_alert_at = (
        datetime.fromisoformat(data["last_alert_at"]) if data.get("last_alert_at") else None
    )
    return over_since, last_alert_at


def set_state(param_name, over_since, last_alert_at):
    data = {
        "over_since": over_since.isoformat() if over_since else None,
        "last_alert_at": last_alert_at.isoformat() if last_alert_at else None,
    }
    ssm_client.put_parameter(Name=param_name, Value=json.dumps(data), Overwrite=True)


def send_alert_email(sender, recipient, temp_f, threshold_f, sustained_minutes):
    subject = f"Thermostat alert: {temp_f:.1f}F (over {threshold_f:.0f}F)"
    body = (
        f"Your Nest thermostat is reading {temp_f:.1f}F, above your "
        f"{threshold_f:.0f}F threshold for {sustained_minutes:.0f} minutes, "
        f"as of {datetime.now(timezone.utc).isoformat()}.\n\nTake a look."
    )
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

    temp_f = get_ambient_temp_f(access_token, project_id, device_id)
    now = datetime.now(timezone.utc)
    over_since, last_alert_at = get_state(state_param_name)
    logger.info("Ambient temperature: %.1fF (threshold %.0fF)", temp_f, threshold_f)

    alert_sent = False
    sustained_minutes = 0.0
    if temp_f > threshold_f:
        if over_since is None:
            over_since = now
            set_state(state_param_name, over_since, None)
            logger.info("Temperature exceeded threshold; starting sustained timer.")
        else:
            sustained_minutes = (now - over_since).total_seconds() / 60
            if sustained_minutes >= sustained_minutes_required:
                if last_alert_at is None:
                    should_alert = True
                else:
                    minutes_since_last_alert = (now - last_alert_at).total_seconds() / 60
                    should_alert = minutes_since_last_alert >= repeat_alert_minutes
                if should_alert:
                    send_alert_email(sender, recipient, temp_f, threshold_f, sustained_minutes)
                    alert_sent = True
                    set_state(state_param_name, over_since, now)
                    logger.info("Alert email sent to %s", recipient)
                else:
                    logger.info(
                        "Sustained over threshold but alerted %.1f min ago (< %.0f min); skipping.",
                        (now - last_alert_at).total_seconds() / 60,
                        repeat_alert_minutes,
                    )
            else:
                logger.info(
                    "Over threshold for %.1f min (< %.0f min required); not alerting yet.",
                    sustained_minutes,
                    sustained_minutes_required,
                )
    else:
        if over_since is not None:
            set_state(state_param_name, None, None)
            logger.info("Temperature back under threshold; resetting sustained timer.")

    return {
        "temperature_f": round(temp_f, 1),
        "sustained_minutes": round(sustained_minutes, 1),
        "alert_sent": alert_sent,
    }
