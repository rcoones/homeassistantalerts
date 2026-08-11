#!/usr/bin/env python3
import aws_cdk as cdk

from nest_alert.nest_alert_stack import NestAlertStack

app = cdk.App()
NestAlertStack(app, "NestTempAlertStack")
app.synth()
