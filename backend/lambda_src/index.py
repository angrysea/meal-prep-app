import base64
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
ADMINS_GROUP_NAME = os.environ.get("ADMINS_GROUP_NAME", "Admins")

# DYNAMODB_ENDPOINT_OVERRIDE lets this run against DynamoDB Local
# (docker-compose) instead of real AWS when testing outside of SAM.
_endpoint = os.environ.get("DYNAMODB_ENDPOINT_OVERRIDE")
_dynamodb = boto3.resource("dynamodb", endpoint_url=_endpoint) if _endpoint else boto3.resource("dynamodb")
table = _dynamodb.Table(TABLE_NAME)

MACRO_FIELDS = ("calories", "proteinG", "carbsG", "fatG")


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
