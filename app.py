#!/usr/bin/env python3
import os

import aws_cdk as cdk

from meal_prep_app.meal_prep_app_stack import MealPrepAppStack

app = cdk.App()
MealPrepAppStack(app, "MealPrepAppStack",
    domain_name="gtxmeals.com",
    # Requested/DNS-validated outside CDK - see README. Issued and imported
    # by ARN here because this account's org-level SCP blocks CloudFormation
    # in us-east-1, where a CloudFront certificate must live.
    certificate_arn="arn:aws:acm:us-east-1:915376882990:certificate/a7279a27-bcb9-4bf8-8ce0-8e999f0aa8f6",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION", "us-east-2"),
    ),
    )

app.synth()
