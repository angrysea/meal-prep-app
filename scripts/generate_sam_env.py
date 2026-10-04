#!/usr/bin/env python3
"""Builds local-env.json for `sam local start-api`.

CDK regenerates Lambda logical IDs on every synth (e.g. ApiFunctionCE271BD4),
so we find the function by its handler instead of hardcoding the ID, and emit
the env-vars file SAM needs to point boto3 at DynamoDB Local.
"""
import json
import pathlib
import sys

STACK_NAME = "MealPrepAppStack"
TEMPLATE_PATH = pathlib.Path(f"cdk.out/{STACK_NAME}.template.json")

LOCAL_ENV = {
    "DYNAMODB_ENDPOINT_OVERRIDE": "http://host.docker.internal:8000",
    # DynamoDB Local checks the access key *looks* like a real one (format,
    # not signature) and rejects arbitrary strings like "local" with
    # UnrecognizedClientException - these are AWS's own documented example
    # credentials, not a real account.
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_DEFAULT_REGION": "us-east-1",
    # SAM's Lambda containers default to a dummy session token; clear it so
    # it doesn't get combined with the access key above into an invalid
    # (mismatched) set of credentials.
    "AWS_SESSION_TOKEN": "",
}


def main():
    if not TEMPLATE_PATH.exists():
        sys.exit(f"{TEMPLATE_PATH} not found - run `cdk synth` first")

    template = json.loads(TEMPLATE_PATH.read_text())
    resources = template["Resources"]

    function_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource.get("Metadata", {}).get("aws:cdk:path", "").endswith("ApiFunction/Resource")
    ]

    if not function_ids:
        sys.exit("No Lambda function found at construct path '.../ApiFunction/Resource'")

    env = {logical_id: LOCAL_ENV for logical_id in function_ids}
    pathlib.Path("local-env.json").write_text(json.dumps(env, indent=2))
    print(f"Wrote local-env.json for: {', '.join(function_ids)}")


if __name__ == "__main__":
    main()
