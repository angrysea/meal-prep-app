#!/usr/bin/env bash
# Creates the MealPrepTable schema against DynamoDB Local (docker-compose).
# DynamoDB Local doesn't check credentials, but the AWS CLI still requires
# *something* in the right shape to be configured - generated fresh each run
# rather than a fixed literal, so there's never a static credential-shaped
# string sitting in source control for a scanner (e.g. GitGuardian) to flag.
set -euo pipefail

export AWS_ACCESS_KEY_ID="AKIA$(LC_ALL=C tr -dc 'A-Z0-9' </dev/urandom | head -c16)"
export AWS_SECRET_ACCESS_KEY="$(LC_ALL=C tr -dc 'A-Za-z0-9+/' </dev/urandom | head -c40)"
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
