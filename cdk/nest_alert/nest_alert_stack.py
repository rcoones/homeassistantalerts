from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ses as ses
from aws_cdk import aws_ssm as ssm
from constructs import Construct


class NestAlertStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        sender_email = self.node.try_get_context("sender_email")
        recipient_email = self.node.try_get_context("recipient_email")
        nest_project_id = self.node.try_get_context("nest_project_id")
        nest_device_id = self.node.try_get_context("nest_device_id")
        temp_threshold_f = str(self.node.try_get_context("temp_threshold_f") or "76")
        schedule_rate_minutes = int(
            self.node.try_get_context("schedule_rate_minutes") or 15
        )
        sustained_minutes = str(self.node.try_get_context("sustained_minutes") or "30")
        repeat_alert_minutes = str(
            self.node.try_get_context("repeat_alert_minutes") or "60"
        )
        setpoint_threshold_f = str(
            self.node.try_get_context("setpoint_threshold_f") or "76"
        )

        for name, value in {
            "sender_email": sender_email,
            "recipient_email": recipient_email,
            "nest_project_id": nest_project_id,
            "nest_device_id": nest_device_id,
        }.items():
            if not value:
                raise ValueError(
                    f"Missing required CDK context value '{name}'. "
                    "Set it in cdk.json or pass -c "
                    f"{name}=<value> to cdk deploy."
                )

        # Holds the Google OAuth client id/secret/refresh token.
        # Populate the real values after deploy via:
        #   aws secretsmanager put-secret-value --secret-id <arn> --secret-string file://secret.json
        google_secret = secretsmanager.Secret(
            self,
            "GoogleNestCredentials",
            description="Google OAuth client id/secret/refresh token for the Nest SDM API",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Sandbox SES requires the sending identity to be verified (a link is
        # emailed to sender_email). If sender == recipient, that one
        # verification covers both send and receive.
        ses_identity = ses.EmailIdentity(
            self,
            "SenderIdentity",
            identity=ses.Identity.email(sender_email),
        )

        # Tracks two independent over-threshold streaks (ambient temp and
        # cooling setpoint), each as {over_since, last_alert_at}, so alerts
        # only fire once sustained (if required), then at most every
        # repeat_alert_minutes.
        empty_streak = '{"over_since": null, "last_alert_at": null}'
        state_param = ssm.StringParameter(
            self,
            "SustainedOverState",
            description="JSON {ambient, setpoint} tracking the current over-threshold streaks",
            string_value=f'{{"ambient": {empty_streak}, "setpoint": {empty_streak}}}',
        )

        fn = _lambda.DockerImageFunction(
            self,
            "NestTempCheckFn",
            code=_lambda.DockerImageCode.from_image_asset("../lambda"),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "SECRET_ARN": google_secret.secret_arn,
                "NEST_PROJECT_ID": nest_project_id,
                "NEST_DEVICE_ID": nest_device_id,
                "SENDER_EMAIL": sender_email,
                "RECIPIENT_EMAIL": recipient_email,
                "TEMP_THRESHOLD_F": temp_threshold_f,
                "SUSTAINED_MINUTES": sustained_minutes,
                "REPEAT_ALERT_MINUTES": repeat_alert_minutes,
                "SETPOINT_THRESHOLD_F": setpoint_threshold_f,
                "STATE_PARAM_NAME": state_param.parameter_name,
            },
        )
        google_secret.grant_read(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[ses_identity.email_identity_arn],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=[state_param.parameter_arn],
            )
        )

        rule = events.Rule(
            self,
            "ScheduleRule",
            schedule=events.Schedule.rate(Duration.minutes(schedule_rate_minutes)),
        )
        rule.add_target(targets.LambdaFunction(fn))

        CfnOutput(self, "SecretArn", value=google_secret.secret_arn)
        CfnOutput(self, "FunctionName", value=fn.function_name)
