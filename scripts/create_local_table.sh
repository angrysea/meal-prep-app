#!/usr/bin/env bash
# Creates the MealPrepTable schema against DynamoDB Local (docker-compose).
# DynamoDB Local doesn't check credentials, but the AWS CLI still requires
# *something* to be configured, so we pass dummy values.
set -euo pipefail

export AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"
export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
export AWS_DEFAULT_REGION="us-east-1"
export AWS_PAGER=""

aws dynamodb create-table \
  --endpoint-url http://localhost:8000 \
  --table-name MealPrepTable \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  2>&1 | grep -v "ResourceInUseException" || true

echo "Local table ready. Verify with:"
echo "  aws dynamodb list-tables --endpoint-url http://localhost:8000"
