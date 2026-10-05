#!/usr/bin/env python3
import os

import aws_cdk as cdk

from meal_prep_app.meal_prep_app_stack import MealPrepAppStack

app = cdk.App()
MealPrepAppStack(app, "MealPrepAppStack",
    domain_name="gtxmeals.com",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION", "us-east-2"),
    ),
    )

app.synth()
