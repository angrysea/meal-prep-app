# Meal Prep Co. — website

Serverless stack on AWS: S3 + CloudFront (static site), API Gateway (HTTP API) +
Lambda (Python 3.12) for the backend, DynamoDB for data, Cognito for customer
accounts. Infrastructure is defined with the AWS CDK (Python).

**Features:** browse the menu (public) · sign up / log in / forgot password ·
a saved account profile (name/email/phone/address, preferred contact
method, notification opt-out) so checkout doesn't ask twice · place an order
(stub checkout — no real payment is taken) · view your own order history ·
an admin page (at the clean `/admin` URL, not linked from the customer nav)
to add/hide/delete menu items, manage a global add-on list, and send a
weekly reminder email/text to subscribed customers.

```
.
├── app.py                        CDK app entrypoint
├── meal_prep_app/
│   └── meal_prep_app_stack.py    All infra: DynamoDB, Cognito, Lambda, HTTP API, S3, CloudFront
├── backend/lambda_src/index.py   Lambda handler: menu, add-ons, orders, profile, admin routes + authz
├── frontend/                     Static site (plain HTML/CSS/JS, no build step)
│   ├── index.html                 Menu + cart + checkout (prefilled from saved profile)
│   ├── login.html                 Sign up / confirm / log in / reset password
│   ├── account.html                Saved contact details (name/email/phone/address)
│   ├── orders.html                 Order history (requires login)
│   ├── admin.html                  Manage menu + add-ons (requires Admins group)
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
- `scripts/create_admin_user.py` does the same thing in one step — creates
  the Cognito user if needed, sets a permanent password, and adds them to
  the Admins group. Looks up the User Pool ID from the stack automatically:
  ```bash
  python scripts/create_admin_user.py --email you@example.com --password 'SomeStrongPass1' \
    --profile gtx-meal-prep --region us-east-2
  ```
  Requires `botocore[crt]` (already in `requirements-dev.txt`): this account's
  `aws login` stores credentials under a `login_session` key in
  `~/.aws/config`, a newer credential provider that plain `botocore` can't
  resolve at all — any direct boto3 script (not just this one) will fail
  with `Unable to locate credentials` without it, even though `aws` CLI
  commands against the same profile work fine.
  There's no separate admin credential store — admin identity is just a
  Cognito user like any customer, distinguished only by group membership.
  A literal username/password of `admin`/`admin` isn't possible: the pool
  requires an email-format username and an 8+ character password with a
  digit (and changing those would mean recreating the pool, destroying
  every existing account).
- `GET /meals` is public; every other route requires a valid Cognito ID token
  (`Authorization: <idToken>` header, no `Bearer ` prefix); `/meals` write
  routes additionally require Admins-group membership, checked in the Lambda.
- Order totals are always recomputed server-side from the current menu —
  client-submitted prices are ignored.

## Menu data model

Each meal has a flat `macros` object (`calories`, `proteinG`, `carbsG`,
`fatG`). Add-ons (e.g. "Large" +$3.00, "Extra Protein" +$1.50) are a single
**global** list — `{addOnId, description, priceCents}` — managed on
`admin.html` and offered as independent, optional checkboxes on *every* meal;
there's no per-meal configuration and no mutual exclusivity (picking "Large"
doesn't prevent also picking something else). At order time, the client sends
only `selectedAddOnIds: [id, ...]` — the Lambda resolves each id against the
*current* global add-on list and sums `priceCents`, the same
never-trust-the-client principle the base price already had. Note: macros are
per-meal only, not affected by add-on selection — if "Large" should actually
change the displayed calories, that'd need its own mechanism; not implemented.

## Weekly reminders

The "Send weekly reminders" button on `/admin`, and the "Send this menu to
customers" button on `menu-flyer.html`, email or text every customer who
hasn't unsubscribed, using their saved `preferredContact` (`email` via SES,
`text` via Twilio). This **will not actually deliver anything yet** without
one-time setup:

1. **Sender domain (done).** Emails are sent from `GTX Meals
   <menu@gtxmeals.com>` (the stack's `FROM_EMAIL`, built from
   `domain_name`). `gtxmeals.com` is a verified domain identity in SES
   (us-east-2) with Easy DKIM, so messages are signed by the domain and pass
   DMARC - unlike sending "from" a `@gmail.com` address through SES, which
   fails Gmail's/Yahoo's checks. The DKIM records live in **Cloudflare**
   (where the domain's DNS is hosted, not Route 53), so this was set up
   outside CDK:
   - Identity: `aws sesv2 create-email-identity --email-identity gtxmeals.com
     --profile gtx-meal-prep --region us-east-2`
   - DNS: the three `<token>._domainkey` CNAMEs it returns, pointing at
     `<token>.dkim.amazonses.com`, set to **DNS only** (not proxied) in
     Cloudflare, plus a `_dmarc` TXT record `v=DMARC1; p=none;`
   - Check: `aws sesv2 get-email-identity --email-identity gtxmeals.com
     --profile gtx-meal-prep --region us-east-2`

   `gtxmeals.com` has no mailbox (no MX record), so **replies go to the
   admin's own address** via Reply-To, resolved from the Admins group in
   Cognito at send time (see `_reply_to()` in `index.py`) - if the admin
   account's email changes, replies follow it automatically, with no SES
   change or redeploy needed. With no admin configured, emails still send,
   just without a Reply-To.
2. SES has **production access** in us-east-2 (granted 2026-10-21; 50,000
   emails a day). New accounts start in the SES sandbox, which only sends to
   verified addresses - production access is requested from Console → SES →
   Account dashboard.
3. **Texting goes through Twilio, not AWS SNS** - this AWS org's SCP blocks
   `sns:Publish`/`sms-voice:*` entirely, so SNS was never usable here. Sign
   up at [twilio.com](https://www.twilio.com), buy a phone number capable of
   SMS, then populate the credentials the Lambda reads from Secrets Manager
   (the secret starts out empty on a fresh deploy):
   ```
   aws secretsmanager put-secret-value --profile gtx-meal-prep \
     --secret-id <TwilioSecretArn from `cdk deploy` output> \
     --secret-string '{"accountSid":"AC...","authToken":"...","fromNumber":"+1..."}'
   ```
   The Auth Token is a real secret (it can send SMS, and rack up charges, on
   the account) - it's never exposed through any API response or the admin
   UI, only reachable by the Lambda's own IAM role.

The endpoints (`POST /reminders/send`, `POST /menu/send`) resolve failures
per-recipient, so one bad address/number doesn't block everyone else - check
the `failed` count and `errors` in the response if something looks off.

## Custom domain (gtxmeals.com)

The site is served from `gtxmeals.com` (and `www.gtxmeals.com`, which
redirects to it) via CloudFront, with DNS hosted at Cloudflare (the
registrar) rather than Route 53 — Cloudflare supports CNAME flattening at
the zone apex, which is what an ALIAS-style root record needs.

The one piece **not** managed by CDK is the ACM certificate: it must be
issued in `us-east-1` for CloudFront regardless of the app's own Region, and
this account's org-level SCP blocks CloudFormation entirely in `us-east-1`
(confirmed directly — `cloudformation:ListStacks` there returns an explicit
deny). So the certificate is requested and DNS-validated outside CDK:

```bash
aws acm request-certificate \
  --domain-name gtxmeals.com --subject-alternative-names www.gtxmeals.com \
  --validation-method DNS --region us-east-1 --profile gtx-meal-prep
```

Then add the two CNAME validation records ACM returns
(`aws acm describe-certificate ... --query Certificate.DomainValidationOptions`)
in Cloudflare's DNS panel (type CNAME, "DNS only"/grey-cloud, not proxied).
Once issued, its ARN is passed into `MealPrepAppStack` as `certificate_arn`
in `app.py` and imported with `acm.Certificate.from_certificate_arn(...)` —
if the cert is ever reissued, update that ARN and redeploy.

Once the distribution has the domain names and certificate attached
(`cdk deploy`), the live DNS records in Cloudflare are:
- `gtxmeals.com` — CNAME (flattened) to the CloudFront distribution domain
  (the `DistributionDomainName` stack output)
- `www.gtxmeals.com` — CNAME to the same distribution domain (the
  `CleanUrlsFunction` CloudFront Function redirects it to the apex)

## Monitoring and alerts

`meal_prep_app/monitoring.py` adds CloudWatch alarms for every service the app
uses, a **GTXMeals** CloudWatch dashboard, and alert emails to `alert_email`
in `app.py` (gtxmeals@gmail.com):

| Service | Alarms (`GTXMeals-*`) |
|---|---|
| Lambda (API) | `ApiErrors`, `ApiThrottles`, `ApiNearTimeout` (over 8 of its 10 seconds) |
| Lambda logs | `ApiLogErrors` - any `ERROR`, timeout or crash line in the API's logs, including errors it recovers from (`print(f"ERROR ...")`) |
| API Gateway | `Api5xx` |
| DynamoDB | `DatabaseErrors` |
| SES | `EmailBounceRate` (5%), `EmailVolumeHigh` (over 300 in a day; normal is about 30 a week) |
| Website + API | `SiteDown`, `ApiDown` - a health-check Lambda loads the site and `GET /settings` every 15 minutes; alarms after 2 failures in a row |

- **Alerts go through SES, not SNS** - the SCP blocks `sns:Publish`. An
  EventBridge rule sends each `GTXMeals-*` alarm change to
  `backend/monitor_src/alerts.py`, which emails from `alerts@gtxmeals.com`:
  once when an alarm goes off (with the latest API log errors, for API
  alarms) and once when it clears.
- **CloudFront and S3** have no alarms of their own: CloudFront's metrics
  live only in us-east-1, where this project can't deploy. `SiteDown` covers
  them end to end.
- **Cognito** publishes no error metrics without its paid advanced security
  features; failures that reach the API show up under `ApiLogErrors`.
- **Budget**: `GTXMeals-Monthly` emails the same address when AWS spend passes
  80% and 100% of $20 a month, or is forecast to pass $20. Credits are left
  out, so it tracks real usage. AWS updates billing data a few times a day,
  so these can lag by up to about a day.
- **Kept to 10 alarms**, CloudWatch's free allowance: DynamoDB throttling,
  SES spam complaints and the monitoring Lambdas' own errors have no alarm
  (throttles and complaints are on the dashboard).
- To check the email path, trigger an alarm by hand (it resets on the next
  evaluation):
  ```
  aws cloudwatch set-alarm-state --profile gtx-meal-prep --alarm-name GTXMeals-ApiErrors \
    --state-value ALARM --state-reason "Testing the alert email"
  ```

## One-time setup

The CDK CLI and its Python bridge (jsii) need Node.js. Use the version in
`.nvmrc` (Node 24 LTS - the newest release jsii supports; it warns on
untested versions like Node 26):

```bash
nvm install            # reads .nvmrc
npm install -g aws-cdk
python3.12 -m venv .venv
make install
```

## Local development loop (no AWS account needed)

1. Start DynamoDB Local and create the table:
   ```bash
   make dynamodb-up
   make dynamodb-table
   ```
2. Run the tests (no Docker or AWS needed):
   ```bash
   make test            # everything
   make test-unit       # CDK assertions + Lambda logic mocked with moto
   make test-frontend   # admin pages in headless Chrome
   ```
   The frontend tests (`tests/frontend/`) drive your installed Google Chrome
   through Playwright against an in-process mock API (`mock_api.py`) - no
   `playwright install` browser download needed. Without Chrome installed
   they're skipped. A failed frontend test saves a screenshot to
   `test-results/`.
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
