#!/usr/bin/env python3
"""Creates or updates a Cognito user and grants them admin access.

There's no separate "admin" credential store in DynamoDB - admin identity
lives in the same Cognito User Pool as every customer account, distinguished
only by membership in the Admins group (see CLAUDE.md / README for why: the
pool requires an email-format username and an 8+ character password with a
digit, so a literal "admin"/"admin" login isn't something this system - or
Cognito itself - can represent).

Usage:
    python scripts/create_admin_user.py --email you@example.com --password 'SomeStrongPass1' \\
        --profile gtx-meal-prep --region us-east-2

Safe to re-run: if the user already exists, this just (re)sets their
password and (re)adds them to the Admins group.
"""
import argparse
import re
import sys

import boto3

DEFAULT_STACK_NAME = "MealPrepAppStack"
DEFAULT_GROUP_NAME = "Admins"


def validate_password(password):
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters (matches the Cognito pool's policy).")
    if not re.search(r"\d", password):
        sys.exit("Password must contain at least one digit (matches the Cognito pool's policy).")


def find_user_pool_id(cfn_client, stack_name):
    outputs = cfn_client.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == "UserPoolId":
            return output["OutputValue"]
    sys.exit(f"No UserPoolId output found on stack '{stack_name}'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Admin's email address (used as the Cognito username).")
    parser.add_argument("--password", required=True, help="Permanent password (8+ chars, at least one digit).")
    parser.add_argument("--profile", help="AWS CLI profile to use.")
    parser.add_argument("--region", default="us-east-2", help="AWS region (default: us-east-2).")
    parser.add_argument("--stack-name", default=DEFAULT_STACK_NAME, help=f"CDK stack name (default: {DEFAULT_STACK_NAME}).")
    parser.add_argument("--user-pool-id", help="Skip the CloudFormation lookup and use this User Pool ID directly.")
    parser.add_argument("--group", default=DEFAULT_GROUP_NAME, help=f"Cognito group to add the user to (default: {DEFAULT_GROUP_NAME}).")
    args = parser.parse_args()

    validate_password(args.password)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cognito = session.client("cognito-idp")

    user_pool_id = args.user_pool_id or find_user_pool_id(session.client("cloudformation"), args.stack_name)

    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=args.email,
            UserAttributes=[
                {"Name": "email", "Value": args.email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",  # we're setting the password directly below, no invite email needed
        )
        print(f"Created user {args.email}.")
    except cognito.exceptions.UsernameExistsException:
        print(f"User {args.email} already exists - updating password and group membership.")

    cognito.admin_set_user_password(
        UserPoolId=user_pool_id, Username=args.email, Password=args.password, Permanent=True,
    )
    cognito.admin_add_user_to_group(
        UserPoolId=user_pool_id, Username=args.email, GroupName=args.group,
    )

    print(f"{args.email} can now log in at the site and has '{args.group}' access.")


if __name__ == "__main__":
    main()
