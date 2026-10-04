import base64
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
ADMINS_GROUP_NAME = os.environ.get("ADMINS_GROUP_NAME", "Admins")
# Must be verified in SES before weekly reminder emails will actually send -
# see README. Placeholder until the real sender address is configured.
FROM_EMAIL = os.environ.get("FROM_EMAIL", "orders@example.com")
SITE_URL = os.environ.get("SITE_URL", "")

# DYNAMODB_ENDPOINT_OVERRIDE lets this run against DynamoDB Local
# (docker-compose) instead of real AWS when testing outside of SAM.
_endpoint = os.environ.get("DYNAMODB_ENDPOINT_OVERRIDE")
_dynamodb = boto3.resource("dynamodb", endpoint_url=_endpoint) if _endpoint else boto3.resource("dynamodb")
table = _dynamodb.Table(TABLE_NAME)
ses = boto3.client("ses")
sns = boto3.client("sns")
cognito_idp = boto3.client("cognito-idp")

MACRO_FIELDS = ("calories", "proteinG", "carbsG", "fatG")
CONTACT_METHODS = ("email", "text")


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    path_params = event.get("pathParameters") or {}

    try:
        if path == "/meals" and method == "GET":
            return list_meals()
        if path == "/meals" and method == "POST":
            return create_meal(event)
        if path.startswith("/meals/") and method == "PUT":
            return update_meal(event, path_params["mealId"])
        if path.startswith("/meals/") and method == "DELETE":
            return delete_meal(event, path_params["mealId"])
        if path == "/addons" and method == "GET":
            return list_addons()
        if path == "/addons" and method == "POST":
            return create_addon(event)
        if path.startswith("/addons/") and method == "DELETE":
            return delete_addon(event, path_params["addOnId"])
        if path == "/orders" and method == "GET":
            return list_orders(event)
        if path == "/orders" and method == "POST":
            return create_order(event)
        if path == "/profile" and method == "GET":
            return get_profile(event)
        if path == "/profile" and method == "PUT":
            return update_profile(event)
        if path == "/reminders/send" and method == "POST":
            return send_weekly_reminders(event)
        if path == "/customers" and method == "GET":
            return list_customers(event)
        if path.startswith("/customers/") and method == "PUT":
            return update_customer(event, path_params["username"])
        if path.startswith("/customers/") and method == "DELETE":
            return delete_customer(event, path_params["username"])
    except AuthError as e:
        return _response(e.status_code, {"message": str(e)})
    except BadRequest as e:
        return _response(400, {"message": str(e)})
    except KeyError as e:
        return _response(400, {"message": f"missing required field: {e}"})

    return _response(404, {"message": f"no route for {method} {path}"})


# ---------- menu ----------

def list_meals():
    items = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": "MEAL"},
    ).get("Items", [])
    return _response(200, {"meals": [_meal_out(item) for item in items]})


def create_meal(event):
    _require_admin(event)
    body = _body(event)
    meal_id = uuid.uuid4().hex[:12]
    item = {
        "PK": "MEAL",
        "SK": meal_id,
        "mealId": meal_id,
        "name": body["name"],
        "description": body.get("description", ""),
        "priceCents": int(body["priceCents"]),
        "available": bool(body.get("available", True)),
        "macros": _parse_macros(body.get("macros")),
    }
    table.put_item(Item=item)
    return _response(201, _meal_out(item))


def update_meal(event, meal_id):
    _require_admin(event)
    body = _body(event)
    existing = table.get_item(Key={"PK": "MEAL", "SK": meal_id}).get("Item")
    if not existing:
        return _response(404, {"message": "meal not found"})

    updated = {
        **existing,
        "name": body.get("name", existing["name"]),
        "description": body.get("description", existing.get("description", "")),
        "priceCents": int(body.get("priceCents", existing["priceCents"])),
        "available": bool(body.get("available", existing.get("available", True))),
        "macros": _parse_macros(body["macros"]) if "macros" in body else existing.get("macros", _parse_macros(None)),
    }
    table.put_item(Item=updated)
    return _response(200, _meal_out(updated))


def delete_meal(event, meal_id):
    _require_admin(event)
    table.delete_item(Key={"PK": "MEAL", "SK": meal_id})
    return _response(204, None)


def _parse_macros(raw):
    raw = raw or {}
    return {field: int(raw.get(field, 0) or 0) for field in MACRO_FIELDS}


def _meal_out(item):
    return {
        "mealId": item["SK"],
        "name": item["name"],
        "description": item.get("description", ""),
        "priceCents": int(item["priceCents"]),
        "available": bool(item.get("available", True)),
        "macros": {field: int(item.get("macros", {}).get(field, 0)) for field in MACRO_FIELDS},
    }


# ---------- add-ons ----------
# A single global list, e.g. "Large" (+$3.00), "Extra Protein" (+$1.50),
# offered as independent optional checkboxes on every meal - not grouped or
# mutually exclusive.

def list_addons():
    items = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": "ADDON"},
    ).get("Items", [])
    return _response(200, {"addOns": [_addon_out(item) for item in items]})


def create_addon(event):
    _require_admin(event)
    body = _body(event)
    addon_id = uuid.uuid4().hex[:12]
    item = {
        "PK": "ADDON",
        "SK": addon_id,
        "addOnId": addon_id,
        "description": body["description"],
        "priceCents": int(body["priceCents"]),
    }
    table.put_item(Item=item)
    return _response(201, _addon_out(item))


def delete_addon(event, addon_id):
    _require_admin(event)
    table.delete_item(Key={"PK": "ADDON", "SK": addon_id})
    return _response(204, None)


def _addon_out(item):
    return {
        "addOnId": item["SK"],
        "description": item["description"],
        "priceCents": int(item["priceCents"]),
    }


# ---------- customer profile ----------
# Saved contact details (name/email/phone/address) so returning customers
# don't have to retype them at checkout every time, plus notification
# preferences used by the weekly-reminder feature below.

PROFILE_TEXT_FIELDS = ("name", "email", "phone", "address")


def get_profile(event):
    claims = _claims(event)
    item = table.get_item(Key={"PK": f"USER#{claims['sub']}", "SK": "PROFILE"}).get("Item")
    return _response(200, _profile_out(item, claims))


def update_profile(event):
    claims = _claims(event)
    body = _body(event)
    preferred_contact = body.get("preferredContact", "email")
    if preferred_contact not in CONTACT_METHODS:
        raise BadRequest(f"preferredContact must be one of {CONTACT_METHODS}")

    item = {
        "PK": f"USER#{claims['sub']}",
        "SK": "PROFILE",
        **{field: body.get(field, "") for field in PROFILE_TEXT_FIELDS},
        "preferredContact": preferred_contact,
        "unsubscribed": bool(body.get("unsubscribed", False)),
    }
    table.put_item(Item=item)
    return _response(200, _profile_out(item, claims))


def _profile_out(item, claims):
    item = item or {}
    return {
        "name": item.get("name", ""),
        # Falls back to the Cognito login email until the customer saves a
        # profile of their own (e.g. the first time they open the account page).
        "email": item.get("email") or claims.get("email", ""),
        "phone": item.get("phone", ""),
        "address": item.get("address", ""),
        "preferredContact": item.get("preferredContact", "email"),
        "unsubscribed": bool(item.get("unsubscribed", False)),
    }


# ---------- customer accounts (admin) ----------
# The authoritative list of "accounts" is Cognito, not DynamoDB - a customer
# who signed up but never saved a profile or placed an order wouldn't appear
# in a DynamoDB-only scan. This lists every Cognito user and merges in their
# profile row (if any) for the contact fields. Admin-only - exposes every
# customer's contact info.

def list_customers(event):
    _require_admin(event)
    customers = []
    kwargs = {"UserPoolId": USER_POOL_ID}
    while True:
        page = cognito_idp.list_users(**kwargs)
        customers.extend(_customer_out(user) for user in page.get("Users", []))
        token = page.get("PaginationToken")
        if not token:
            break
        kwargs["PaginationToken"] = token
    return _response(200, {"customers": customers})


def update_customer(event, username):
    _require_admin(event)
    sub = _resolve_sub(username)
    body = _body(event)
    preferred_contact = body.get("preferredContact", "email")
    if preferred_contact not in CONTACT_METHODS:
        raise BadRequest(f"preferredContact must be one of {CONTACT_METHODS}")

    item = {
        "PK": f"USER#{sub}",
        "SK": "PROFILE",
        **{field: body.get(field, "") for field in PROFILE_TEXT_FIELDS},
        "preferredContact": preferred_contact,
        "unsubscribed": bool(body.get("unsubscribed", False)),
    }
    table.put_item(Item=item)
    return _response(200, {
        "username": username,
        "name": item["name"],
        "email": item["email"],
        "phone": item["phone"],
        "address": item["address"],
        "preferredContact": item["preferredContact"],
        "unsubscribed": item["unsubscribed"],
    })


def delete_customer(event, username):
    """Deletes the Cognito account (revokes login) and the saved profile row.
    Past orders are kept - they're a business record, not part of "the account".
    """
    _require_admin(event)
    sub = _resolve_sub(username)
    table.delete_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"})
    cognito_idp.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    return _response(204, None)


def _resolve_sub(username):
    try:
        user = cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    except cognito_idp.exceptions.UserNotFoundException:
        raise BadRequest(f"no such customer: {username}")
    attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
    return attrs["sub"]


def _customer_out(cognito_user):
    attrs = {a["Name"]: a["Value"] for a in cognito_user.get("Attributes", [])}
    sub = attrs.get("sub")
    username = cognito_user["Username"]
    profile = table.get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}).get("Item") or {}
    return {
        # The stable identifier for edit/delete calls - never shown as an
        # editable field itself (that's "email" below, which can diverge:
        # a customer may set a different contact email than their login).
        "username": username,
        "name": profile.get("name", ""),
        "email": profile.get("email") or attrs.get("email", username),
        "phone": profile.get("phone", ""),
        "address": profile.get("address", ""),
        "preferredContact": profile.get("preferredContact", "email"),
        "unsubscribed": bool(profile.get("unsubscribed", False)),
    }


# ---------- weekly reminders ----------
# Admin-triggered broadcast to every customer who hasn't unsubscribed, via
# whichever channel (email/text) they prefer. Runs as a single on-demand
# Lambda invocation - no schedule is set up, admin clicks the button.

REMINDER_SUBJECT = "This week's menu is up at GTX Meals!"


def _reminder_message():
    base = (
        "This week's menu is up at GTX Meals! Order by Friday 2PM for Sunday "
        "prep, Monday pickup at the gym."
    )
    if SITE_URL:
        return f"{base} Order now: {SITE_URL}"
    return base


def send_weekly_reminders(event):
    _require_admin(event)
    message = _reminder_message()

    sent_email = sent_text = skipped_unsubscribed = failed = 0
    errors = []

    for profile in _scan_all_profiles():
        if profile.get("unsubscribed"):
            skipped_unsubscribed += 1
            continue

        channel = profile.get("preferredContact", "email")
        try:
            if channel == "text":
                phone = profile.get("phone")
                if not phone:
                    raise ValueError("no phone number on file")
                sns.publish(PhoneNumber=phone, Message=message)
                sent_text += 1
            else:
                email = profile.get("email")
                if not email:
                    raise ValueError("no email on file")
                ses.send_email(
                    Source=FROM_EMAIL,
                    Destination={"ToAddresses": [email]},
                    Message={
                        "Subject": {"Data": REMINDER_SUBJECT},
                        "Body": {"Text": {"Data": message}},
                    },
                )
                sent_email += 1
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't abort the batch
            failed += 1
            errors.append(f"{profile.get('PK')}: {e}")

    return _response(200, {
        "sentEmail": sent_email,
        "sentText": sent_text,
        "skippedUnsubscribed": skipped_unsubscribed,
        "failed": failed,
        "errors": errors[:10],  # cap so one noisy failure mode doesn't blow up the response
    })


def _scan_all_profiles():
    """Every customer's PROFILE item. A full-table Scan with a filter is the
    simplest option at this scale (one admin click, infrequent); a GSI would
    be the next step if the customer list grows large enough for Scan's
    read cost to matter.
    """
    profiles = []
    scan_kwargs = {
        "FilterExpression": "begins_with(PK, :userPrefix) AND SK = :sk",
        "ExpressionAttributeValues": {":userPrefix": "USER#", ":sk": "PROFILE"},
    }
    while True:
        page = table.scan(**scan_kwargs)
        profiles.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return profiles


# ---------- orders ----------

def list_orders(event):
    user_id = _user_id(event)
    items = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": f"USER#{user_id}", ":sk": "ORDER#"},
        ScanIndexForward=False,  # most recent first
    ).get("Items", [])
    return _response(200, {"orders": [_order_out(item) for item in items]})


def create_order(event):
    user_id = _user_id(event)
    body = _body(event)
    requested_items = body.get("items") or []
    if not requested_items:
        return _response(400, {"message": "order must include at least one item"})

    # Prices (and add-ons) are recomputed from the current menu/add-on list,
    # never trusted from the client.
    line_items = []
    total_cents = 0
    for requested in requested_items:
        meal_id = requested["mealId"]
        quantity = int(requested.get("quantity", 1))
        meal = table.get_item(Key={"PK": "MEAL", "SK": meal_id}).get("Item")
        if not meal:
            return _response(400, {"message": f"unknown mealId: {meal_id}"})

        resolved_addons, addons_total_cents = _resolve_addons(requested.get("selectedAddOnIds") or [])

        unit_price_cents = int(meal["priceCents"]) + addons_total_cents
        total_cents += unit_price_cents * quantity
        line_items.append({
            "mealId": meal_id,
            "name": meal["name"],
            "unitPriceCents": unit_price_cents,
            "quantity": quantity,
            "selectedAddOns": resolved_addons,
        })

    order_id = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"ORDER#{created_at}#{order_id}",
        "orderId": order_id,
        "createdAt": created_at,
        "items": line_items,
        "totalCents": total_cents,
        "deliveryName": body.get("deliveryName", ""),
        "deliveryEmail": body.get("deliveryEmail", ""),
        "deliveryPhone": body.get("deliveryPhone", ""),
        "deliveryAddress": body.get("deliveryAddress", ""),
        "status": "placed",  # stub checkout - no real payment is taken
    }
    table.put_item(Item=item)
    return _response(201, _order_out(item))


def _resolve_addons(requested_addon_ids):
    """Validates the client's add-on id picks against the current global
    add-on list and returns (resolved add-ons with description/price
    snapshotted, total price delta in cents). Raises BadRequest for any id
    that doesn't currently exist.
    """
    resolved = []
    total_cents = 0
    for addon_id in requested_addon_ids:
        addon = table.get_item(Key={"PK": "ADDON", "SK": addon_id}).get("Item")
        if not addon:
            raise BadRequest(f"unknown add-on: {addon_id}")
        price_cents = int(addon["priceCents"])
        total_cents += price_cents
        resolved.append({
            "addOnId": addon_id,
            "description": addon["description"],
            "priceCents": price_cents,
        })
    return resolved, total_cents


def _order_out(item):
    return {
        "orderId": item["orderId"],
        "createdAt": item["createdAt"],
        "items": [dict(i) for i in item["items"]],
        "totalCents": int(item["totalCents"]),
        "deliveryName": item.get("deliveryName", ""),
        "deliveryEmail": item.get("deliveryEmail", ""),
        "deliveryPhone": item.get("deliveryPhone", ""),
        "deliveryAddress": item.get("deliveryAddress", ""),
        "status": item.get("status", "placed"),
    }


# ---------- auth helpers ----------

class AuthError(Exception):
    def __init__(self, message, status_code=401):
        super().__init__(message)
        self.status_code = status_code


class BadRequest(Exception):
    pass


def _claims(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    if not claims:
        raise AuthError("authentication required", 401)
    return claims


def _user_id(event):
    return _claims(event)["sub"]


def _require_admin(event):
    claims = _claims(event)
    # Cognito serializes group membership as a stringified list in the JWT claims
    # surfaced by API Gateway, so this checks substring membership rather than
    # parsing it as JSON.
    groups = claims.get("cognito:groups", "")
    if ADMINS_GROUP_NAME not in groups:
        raise AuthError("admin privileges required", 403)


# ---------- plumbing ----------

def _body(event):
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    return json.loads(raw_body)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o)
        return super().default(o)


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder) if body is not None else "",
    }
