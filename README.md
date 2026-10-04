# Meal Prep Co. — website

Serverless stack on AWS: S3 + CloudFront (static site), API Gateway (HTTP API) +
Lambda (Python 3.12) for the backend, DynamoDB for data, Cognito for customer
accounts. Infrastructure is defined with the AWS CDK (Python).

**Features:** browse the menu (public) · sign up / log in · place an order
(stub checkout — no real payment is taken) · view your own order history ·
an admin-only page to add/hide/delete menu items.

```
.
├── app.py                        CDK app entrypoint
├── meal_prep_app/
│   └── meal_prep_app_stack.py    All infra: DynamoDB, Cognito, Lambda, HTTP API, S3, CloudFront
├── backend/lambda_src/index.py   Lambda handler: menu, orders, admin routes + authz
├── frontend/                     Static site (plain HTML/CSS/JS, no build step)
│   ├── index.html                 Menu + cart + checkout
│   ├── login.html                 Sign up / confirm / log in
│   ├── orders.html                 Order history (requires login)
│   ├── admin.html                  Manage menu (requires Admins group)
│   └── js/cognito.js               Calls Cognito's public API directly via fetch — no SDK/build step
├── tests/unit/                   CDK assertion tests + Lambda handler tests (pytest + moto)
├── docker-compose.yml            DynamoDB Local for offline testing
├── scripts/                      Local dev helper scripts
└── Makefile                      Shortcuts for the commands below
```

## Auth model

- Customers self-sign-up via Cognito (email + password, email confirmation code).
- Admin access is **never** self-granted. After someone signs up, make them an
  admin with:
  ```bash
  aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> \
    --username <email> --group-name Admins --profile gtx-meal-prep --region us-east-2
  ```
  They must log out and back in afterward — group membership is baked into the
  ID token at sign-in time, not checked live.
- `GET /meals` is public; every other route requires a valid Cognito ID token
  (`Authorization: <idToken>` header, no `Bearer ` prefix); `/meals` write
  routes additionally require Admins-group membership, checked in the Lambda.
- Order totals are always recomputed server-side from the current menu —
  client-submitted prices are ignored.

## Menu data model

Each meal has a flat `macros` object (`calories`, `proteinG`, `carbsG`, `fatG`)
and an admin-defined, fully generic `optionGroups` list — there's no hardcoded
notion of "size" or "add-on". A group is `{id, name, selectionType: "single" |
"multi", required, options: [{id, label, priceDeltaCents}]}`; admins build
these in the "Add a meal" form on `admin.html`. At order time, the client
sends only `{groupId, optionId}` pairs — the Lambda resolves them against the
meal's *current* option groups, validates required/single-vs-multi rules, and
computes the price from `priceDeltaCents`, the same never-trust-the-client
principle as the base price. Note: macros are per-meal, not per-option — if a
"Large" selection should actually change calories (as it does on the flyer
this was modeled from), that'd need a `macrosDelta` added to the option shape,
analogous to `priceDeltaCents`; not implemented yet.

## One-time setup

```bash
python3.12 -m venv .venv
make install
```

## Local development loop (no AWS account needed)

1. Start DynamoDB Local and create the table:
   ```bash
   make dynamodb-up
   make dynamodb-table
   ```
2. Run unit tests (CDK assertions + Lambda logic mocked with moto, no Docker needed):
   ```bash
   make test
   ```
3. Run the full local stack — synthesizes the CDK app to CloudFormation and
   serves it through SAM's local API Gateway + Lambda emulator, wired to
   DynamoDB Local:
   ```bash
   make local-api
   ```
   Then in another terminal:
   ```bash
   curl http://127.0.0.1:3000/meals
   ```
   Note: SAM local doesn't enforce the Cognito JWT authorizer, so routes other
   than `GET /meals` will 401 locally (no real claims to pass through). Those
   routes are covered by the moto-based tests in `test_handler.py` instead,
   which construct the JWT claims directly the way API Gateway would.
4. When done: `make dynamodb-down`

## Deploying to AWS

This project is on AWS's **new account experience** (a "project," not a
traditional IAM account — see `CLAUDE.md` for the full rules). That means:

- Sign-in is via `aws login --profile gtx-meal-prep` (browser-based; no IAM
  users/access keys to manage — human access and SCPs/RCPs are managed by AWS).
- There's one fixed Region per project, assigned to your contact address —
  this project's is `us-east-2`. CDK must deploy there; resources can't be
  created in other Regions (CloudFront/WAF/CloudWatch Logs in `us-east-1` are
  the only Region-agnostic exception, and this stack doesn't use them).
- Billing, spend limits, and team members are managed at
  [settings.aws.com](https://settings.aws.com/), not the IAM/Billing console.

```bash
source .venv/bin/activate
cdk bootstrap aws://915376882990/us-east-2 --profile gtx-meal-prep
cdk deploy --profile gtx-meal-prep
```

If AWS access suddenly stops working with errors that previously succeeded,
check for a spend limit pause in AWS Settings > Billing before debugging IAM.
