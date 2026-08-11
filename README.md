# Nest Thermostat Temp Alert

EventBridge schedule -> Lambda (Docker container, Python) -> checks the Nest
thermostat via the Google Smart Device Management (SDM) API -> emails an
alert via SES to your own address if either:
- the **ambient temperature** is sustained over threshold (default 76F), or
- the **cooling setpoint** is over threshold (default 76F) — e.g. someone
  bumped the thermostat up.

## Architecture

- **EventBridge Rule**: runs on a fixed rate (default every 15 min).
- **Lambda (container image)**: refreshes a Google OAuth access token from a
  stored refresh token, calls the SDM API for the device's
  `ambientTemperatureCelsius` and `ThermostatTemperatureSetpoint.coolCelsius`,
  converts both to F, and evaluates each against its own threshold.
- **Secrets Manager**: holds `google_client_id` / `google_client_secret` /
  `google_refresh_token`. Never committed to the repo.
- **SES**: sends the alert email. Sender identity must be verified (click
  the link SES emails you). If sender == recipient (the default here), one
  verification covers both, and you can stay in the SES sandbox indefinitely
  since you're only ever emailing yourself.
- **SSM Parameter Store**: holds a JSON parameter `{ambient, setpoint}`,
  each an independent `{over_since, last_alert_at}` streak.

Alert timing:
- **Ambient temp**: first alert fires once the temp has been continuously
  over threshold for `sustained_minutes` (default 30) — a brief spike won't
  page you. After that, another alert fires only once
  `repeat_alert_minutes` (default 60) has passed since the last one, as
  long as it's still over threshold. Dropping back at/under threshold
  resets the streak (next time it goes over, the sustained timer restarts).
- **Cooling setpoint**: alerts immediately (no sustained wait — it's a
  discrete change, not a fluctuating reading), then throttled to at most
  once per `repeat_alert_minutes` while it stays over threshold. Only
  evaluated when the thermostat reports a cooling setpoint (i.e. in COOL or
  HEATCOOL mode).

## Prerequisites

1. **Docker Desktop WSL integration** enabled for this distro (Docker
   Desktop -> Settings -> Resources -> WSL Integration -> toggle this
   distro on). Needed so `cdk deploy` can build/push the Lambda image.
2. **Python venv support**:
   ```
   sudo apt-get update && sudo apt-get install -y python3-pip python3.12-venv
   ```
3. AWS CLI already configured (confirmed working: account 304196588227 via
   SSO).
4. Node/npm in WSL (installed via nvm) and `aws-cdk` CLI (`npm install -g
   aws-cdk`) — already done.

## One-time setup

```bash
cd cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Bootstrap CDK in this account/region (only needed once ever per account+region)
cdk bootstrap
```

Copy the context template and fill in your real values (`cdk.json` is
gitignored — it holds your email address and Nest project/device IDs, which
aren't credentials but also don't belong in a shared repo):

```bash
cp cdk.json.example cdk.json
```

Then edit `cdk.json`'s `context` block:
- `nest_project_id` — your Google Device Access project ID
- `nest_device_id` — the thermostat's device ID (last path segment from the
  SDM API's device `name` field)
- `sender_email` / `recipient_email` — your email address
- `temp_threshold_f` — default 76
- `schedule_rate_minutes` — default 15
- `sustained_minutes` — default 30
- `repeat_alert_minutes` — default 60
- `setpoint_threshold_f` — default 76

## Deploy

```bash
cd cdk
source .venv/bin/activate
cdk deploy
```

This creates the Secrets Manager secret (empty placeholder), the SES
identity (check your inbox and click the verification link), the Lambda,
and the EventBridge rule.

## Populate the Google OAuth secret

Do this yourself in your own terminal (don't paste real secrets into a chat
session — copy `secret.example.json`, fill in real values, then run):

```bash
cp secret.example.json secret.json
# edit secret.json with your real client_id / client_secret / refresh_token
aws secretsmanager put-secret-value \
  --secret-id "$(aws cloudformation describe-stacks --stack-name NestTempAlertStack \
      --query 'Stacks[0].Outputs[?OutputKey==`SecretArn`].OutputValue' --output text)" \
  --secret-string file://secret.json
rm secret.json
```

## Test it

```bash
FN=$(aws cloudformation describe-stacks --stack-name NestTempAlertStack \
    --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' --output text)
aws lambda invoke --function-name "$FN" /tmp/out.json && cat /tmp/out.json
```

Check CloudWatch Logs (`/aws/lambda/<FunctionName>`) for details if
something goes wrong (bad refresh token, unverified SES identity, wrong
project/device ID, etc).

## Tear down

```bash
cd cdk
source .venv/bin/activate
cdk destroy
```
