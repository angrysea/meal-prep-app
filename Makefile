.PHONY: install synth test dynamodb-up dynamodb-down dynamodb-table local-api clean

VENV := .venv/bin

install:
	$(VENV)/pip install -r requirements.txt -r requirements-dev.txt

synth:
	PATH="$(abspath $(VENV)):$$PATH" cdk synth --no-staging

test:
	$(VENV)/pytest

dynamodb-up:
	docker compose up -d dynamodb-local

dynamodb-down:
	docker compose down

dynamodb-table:
	./scripts/create_local_table.sh

# Full local loop: synth CDK -> generate SAM env file -> start API Gateway
# emulator wired to DynamoDB Local. Requires dynamodb-up + dynamodb-table first.
# DOCKER_HOST is resolved explicitly because SAM's bundled Docker SDK doesn't
# always pick up Docker Desktop's active context (e.g. desktop-linux) on its own.
local-api: synth
	$(VENV)/python scripts/generate_sam_env.py
	DOCKER_HOST="$$(docker context inspect --format '{{.Endpoints.docker.Host}}')" \
		sam local start-api -t cdk.out/MealPrepAppStack.template.json --env-vars local-env.json

clean:
	rm -rf cdk.out local-env.json
