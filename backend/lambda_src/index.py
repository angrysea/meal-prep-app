import base64
import hashlib
import hmac
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
ADMINS_GROUP_NAME = os.environ.get("ADMINS_GROUP_NAME", "Admins")
SITE_URL = os.environ.get("SITE_URL", "")
# Secrets Manager ARN holding {"accountSid", "authToken", "fromNumber"} - see
# _send_text_via_twilio. Populated after deploy; see README.
TWILIO_SECRET_ARN = os.environ.get("TWILIO_SECRET_ARN", "")
# Secrets Manager ARN holding a plain, auto-generated random string used as
# an HMAC key to sign unsubscribe links (see _unsubscribe_token) - no manual
# setup needed, unlike TWILIO_SECRET_ARN.
UNSUBSCRIBE_SECRET_ARN = os.environ.get("UNSUBSCRIBE_SECRET_ARN", "")
# The API Gateway's own base URL - unsubscribe links are clicked straight out
# of an email client with no JS session, so they have to hit this API
# directly rather than the static site's domain (SITE_URL).
API_URL = os.environ.get("API_URL", "")
# Sender for customer emails, e.g. "GTX Meals <menu@gtxmeals.com>" - an
# address on the site's SES-verified domain. Empty when the stack has no
# custom domain; sending email then fails up front (see _from_email).
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")

# DYNAMODB_ENDPOINT_OVERRIDE lets this run against DynamoDB Local
# (docker-compose) instead of real AWS when testing outside of SAM.
_endpoint = os.environ.get("DYNAMODB_ENDPOINT_OVERRIDE")
_dynamodb = boto3.resource("dynamodb", endpoint_url=_endpoint) if _endpoint else boto3.resource("dynamodb")
table = _dynamodb.Table(TABLE_NAME)
ses = boto3.client("ses")
secretsmanager = boto3.client("secretsmanager")
cognito_idp = boto3.client("cognito-idp")

MACRO_FIELDS = ("calories", "proteinG", "carbsG", "fatG")
CONTACT_METHODS = ("email", "text")
ORDER_STATUSES = ("placed", "cancelled", "prepared", "delivered")
# Recorded by the admin on the Orders page - checkout takes no payment, so
# every order starts "unpaid". Separate from status: an order can be paid
# before or after it's prepared.
PAYMENT_STATUSES = ("unpaid", "venmo", "cash", "free")
PAID_METHODS = ("venmo", "cash")
# How much the admin recorded as received (amountPaidCents). Only counts for
# a paid method; "free" owes nothing and "unpaid" has paid nothing.
MAX_AMOUNT_PAID_CENTS = 100_000_00


def _amount_paid(order):
    return int(order.get("amountPaidCents", 0) or 0) if order.get("payment") in PAID_METHODS else 0


def _balance_cents(order):
    """What the customer still owes (positive) or should get back
    (negative). Zero for free orders and for unpaid ones (the whole total is
    simply due)."""
    if order.get("payment") not in PAID_METHODS:
        return 0
    if order.get("status") == "cancelled":
        return -_amount_paid(order)
    return int(order.get("totalCents", 0)) - _amount_paid(order)
MEAL_SUGGESTIONS_MAX = 1000

# A single global value: the next date meals will be ready for pickup. Stored
# as one DynamoDB item rather than per-meal/per-order, since it's set once by
# the admin on the Builder page and every order placed before it changes again
# is prepped for that same date.
SETTINGS_KEY = {"PK": "SETTINGS", "SK": "GLOBAL"}
# Customers can't place an order within this many days of the ready date -
# the kitchen needs lead time to shop/prep.
# Ordering is open Monday 11:00 AM to Friday 4:00 PM, Central time. Customers
# are told the deadline is Friday 2:00 PM - the extra two hours are a quiet
# grace period. The admin can force ordering open or closed instead (e.g. for
# a holiday week) with the orderingMode setting.
ORDERING_OPENS = (0, time(11, 0))  # (weekday, time) - Monday
ORDERING_CLOSES = (4, time(16, 0))  # Friday
ORDERING_MODES = ("auto", "open", "closed")
# The ready date (pickup day) is worked out from the same schedule: once
# ordering opens Monday 11:00 AM it's the following Monday. The admin can
# override it for an unusual week (e.g. a Tuesday pickup after a holiday) -
# an override only replaces the automatic Monday it's within this many days
# of, so it can't outlive its week.
READY_DATE_OVERRIDE_DAYS = 3


def handler(event, context):
    # Scheduled by EventBridge, not an API request (see run_job).
    if "job" in event:
        return run_job(event["job"], context)
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
        if path == "/settings" and method == "GET":
            return get_settings()
        if path == "/settings" and method == "PUT":
            return update_settings(event)
        if path == "/addons" and method == "GET":
            return list_addons()
        if path == "/addons" and method == "POST":
            return create_addon(event)
        if path.startswith("/addons/") and method == "PUT":
            return update_addon(event, path_params["addOnId"])
        if path.startswith("/addons/") and method == "DELETE":
            return delete_addon(event, path_params["addOnId"])
        if path == "/extras" and method == "GET":
            return list_extras()
        if path == "/extras" and method == "POST":
            return create_extra(event)
        if path.startswith("/extras/") and method == "PUT":
            return update_extra(event, path_params["extraId"])
        if path.startswith("/extras/") and method == "DELETE":
            return delete_extra(event, path_params["extraId"])
        if path == "/orders" and method == "GET":
            return list_orders(event)
        if path == "/orders" and method == "POST":
            return create_order(event)
        if path.startswith("/orders/") and method == "PUT":
            return cancel_order(event, path_params["orderId"])
        if path == "/admin/orders" and method == "GET":
            return list_admin_orders(event)
        if path.startswith("/admin/orders/") and method == "PUT":
            return update_order_status(event, path_params["sub"], path_params["orderId"])
        if path == "/profile" and method == "GET":
            return get_profile(event)
        if path == "/profile" and method == "PUT":
            return update_profile(event)
        if path == "/reminders/send" and method == "POST":
            return send_weekly_reminders(event)
        if path == "/menu/send" and method == "POST":
            return send_menu_notification(event)
        if path == "/unsubscribe" and method == "GET":
            return unsubscribe(event)
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


# ---------- global settings ----------
# Ordering schedule, ready date override and SMS switch, kept as their own
# small item/route rather than folded into meals, since they're business-wide
# values with their own
# lifecycle (admin sets it on the Builder page, every order placed before it
# next changes is prepped for that date).

def _utcnow():
    # Its own function so tests can pin the clock.
    return datetime.now(timezone.utc)


def _central(now_utc):
    """now_utc in US Central time. US daylight saving rules are worked out
    here rather than via zoneinfo, so the Lambda doesn't depend on the
    runtime shipping a time zone database: CDT (UTC-5) from 2:00 AM on the
    second Sunday in March to 2:00 AM on the first Sunday in November,
    otherwise CST (UTC-6)."""
    def nth_sunday(month, n):
        first = date(now_utc.year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)

    dst_starts = datetime.combine(nth_sunday(3, 2), time(8, 0), timezone.utc)  # 2:00 AM CST
    dst_ends = datetime.combine(nth_sunday(11, 1), time(7, 0), timezone.utc)  # 2:00 AM CDT
    offset = -5 if dst_starts <= now_utc < dst_ends else -6
    return now_utc.astimezone(timezone(timedelta(hours=offset)))


def _week_minutes(weekday, at):
    return weekday * 24 * 60 + at.hour * 60 + at.minute


def _in_ordering_window(local):
    now = _week_minutes(local.weekday(), local.time())
    return _week_minutes(*ORDERING_OPENS) <= now < _week_minutes(*ORDERING_CLOSES)


def _auto_ready_date(local, forced_open=False):
    """The Monday that orders placed right now are for: this week's Monday
    until ordering opens at 11:00 AM, then the following Monday. While
    ordering is forced open it's never today - nothing can be ordered for a
    pickup already underway."""
    monday = local.date() - timedelta(days=local.weekday())
    before_opening = local.weekday() == ORDERING_OPENS[0] and local.time() < ORDERING_OPENS[1]
    ready = monday if before_opening else monday + timedelta(days=7)
    if forced_open and ready <= local.date():
        ready += timedelta(days=7)
    return ready


def _ready_date_override(settings):
    # Stored as nextReadyDate before the ready date was automatic - a value
    # saved then reads as an override (and is only used in its own week).
    return settings.get("readyDateOverride", settings.get("nextReadyDate", "")) or ""


def _ready_dates(settings, now_utc=None):
    """(ready date in effect, automatic ready date, whether the override is
    being used), all as YYYY-MM-DD."""
    local = _central(now_utc or _utcnow())
    auto = _auto_ready_date(local, forced_open=settings.get("orderingMode") == "open")
    override = _ready_date_override(settings)
    # The same date as the automatic one isn't an override - e.g. a date set
    # by hand before the ready date was automatic.
    if override and override != auto.isoformat():
        days_off = abs((datetime.strptime(override, "%Y-%m-%d").date() - auto).days)
        if days_off <= READY_DATE_OVERRIDE_DAYS:
            return override, auto.isoformat(), True
    return auto.isoformat(), auto.isoformat(), False


def _ordering_status(settings, now_utc=None):
    """Whether customers can order right now, and why:
    reason is "schedule" (the Monday-Friday window), "forced" (the admin's
    override), or "ready-date-passed" (the ready date in effect is today or
    earlier - nothing can be ordered for a pickup already underway)."""
    local = _central(now_utc or _utcnow())
    mode = settings.get("orderingMode") or "auto"
    if mode == "auto":
        is_open, reason = _in_ordering_window(local), "schedule"
    else:
        is_open, reason = mode == "open", "forced"
    ready_date, _, _ = _ready_dates(settings, now_utc)
    if is_open and ready_date <= local.date().isoformat():
        is_open, reason = False, "ready-date-passed"
    return {"open": is_open, "reason": reason}


def _change_deadline_passed(ready_date_str, local):
    """Customers can edit or cancel an order until 4:00 PM (Central) on the
    Friday before its ready date - the same moment ordering closes, so the
    grace period is the same too. They're told 2:00 PM."""
    ready = datetime.strptime(ready_date_str, "%Y-%m-%d").date()
    friday = ready - timedelta(days=(ready.weekday() - 4) % 7 or 7)
    return (local.date(), local.time()) >= (friday, ORDERING_CLOSES[1])


def _order_changeable(item, settings, now_utc=None):
    """Whether the customer can still edit or cancel this order. "Edit"
    cancels the order and reloads it into the cart, so ordering also has to
    be open right now - otherwise they couldn't place it again."""
    if item.get("status", "placed") != "placed":
        return False
    if not _ordering_status(settings, now_utc)["open"]:
        return False
    ready_date = item.get("readyDate", "")
    return not ready_date or not _change_deadline_passed(ready_date, _central(now_utc or _utcnow()))


def get_settings():
    item = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    try:
        emails = _admin_emails()
        contact_email = emails[0] if emails else ""
    except Exception as e:  # noqa: BLE001 - this is public and loaded on every page view;
        # a Cognito hiccup should leave the About page's contact line blank,
        # not break menu browsing for every customer. Logged as an ERROR so
        # the log-errors alarm still reports it.
        print(f"ERROR looking up the admin contact email: {e!r}")
        contact_email = ""
    return _response(200, {
        **_settings_out(item),
        # Shown on the About Us page's "contact us" line - the admin's own
        # address, same source as _reply_to(), so it never drifts out of
        # sync with whoever actually holds the Admins-group account.
        "contactEmail": contact_email,
    })


def _settings_out(item):
    ready_date, auto_ready_date, override_used = _ready_dates(item)
    return {
        # The ready date in effect - what new orders are for, which dated
        # meals are on the weekly menu, and the Prep List's default date.
        "nextReadyDate": ready_date,
        "readyDateAuto": auto_ready_date,
        # As saved, even when it's not for the current week (then unused) -
        # except when it's simply the automatic date, which isn't an override.
        "readyDateOverride": "" if _ready_date_override(item) == auto_ready_date else _ready_date_override(item),
        "readyDateOverrideUsed": override_used,
        # Off by default - SMS costs real money per message and this
        # account's SNS access is currently blocked at the AWS org level
        # (see README), so there's nothing to accidentally send to yet.
        "smsEnabled": bool(item.get("smsEnabled", False)),
        "orderingMode": item.get("orderingMode") or "auto",
        "ordering": _ordering_status(item),
    }


def update_settings(event):
    _require_admin(event)
    body = _body(event)
    # Merged with the existing item, not blindly overwritten - the Builder
    # page saves each setting from its own button/form, so a request that
    # only names one field (e.g. just readyDateOverride) must leave the other
    # (e.g. smsEnabled) as it was, not reset it to a default.
    existing = table.get_item(Key=SETTINGS_KEY).get("Item") or {}

    # Blank clears the override (back to the automatic Monday).
    override = (body.get("readyDateOverride", _ready_date_override(existing)) or "").strip()
    if override:
        try:
            datetime.strptime(override, "%Y-%m-%d")
        except ValueError:
            raise BadRequest("readyDateOverride must be in YYYY-MM-DD format")
    sms_enabled = bool(body.get("smsEnabled", existing.get("smsEnabled", False)))
    ordering_mode = body.get("orderingMode", existing.get("orderingMode") or "auto")
    if ordering_mode not in ORDERING_MODES:
        raise BadRequest(f"orderingMode must be one of {ORDERING_MODES}")

    item = {**SETTINGS_KEY, "readyDateOverride": override, "smsEnabled": sms_enabled, "orderingMode": ordering_mode}
    table.put_item(Item=item)
    return _response(200, _settings_out(item))


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
        "addOnDetails": _parse_addon_details(body.get("addOnDetails")),
        "staple": bool(body.get("staple", False)),
        "weekOf": _parse_week_of(body.get("weekOf", "")),
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
        "addOnDetails": (
            _parse_addon_details(body["addOnDetails"]) if "addOnDetails" in body
            else existing.get("addOnDetails", {})
        ),
        "staple": bool(body.get("staple", existing.get("staple", False))),
        "weekOf": _parse_week_of(body["weekOf"]) if "weekOf" in body else existing.get("weekOf", ""),
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


def _parse_addon_details(raw):
    """This meal's own copy of each add-on, keyed by addOnId: its name and
    price (the price customers pay for it on this meal - Manage menu starts
    it at the add-on's price), what it means for this dish, and the macros
    of the whole meal with it. The name and price are kept on the meal so
    they survive the add-on being deleted. Add-ons not listed fall back to
    the add-on's own name, price and default description, and no macros."""
    if not isinstance(raw or {}, dict):
        raise BadRequest("addOnDetails must be an object keyed by addOnId")
    return {str(addon_id): _parse_addon_detail(detail or {}) for addon_id, detail in (raw or {}).items()}


def _parse_addon_detail(detail):
    parsed = {
        "name": (detail.get("name") or "").strip(),
        "description": (detail.get("description") or "").strip(),
        "macros": _parse_macros(detail.get("macros")),
    }
    # Left out (falls back to the add-on's price) when not given.
    if detail.get("priceCents") not in (None, ""):
        price_cents = int(detail["priceCents"])
        if price_cents < 0:
            raise BadRequest("add-on price can't be negative")
        parsed["priceCents"] = price_cents
    return parsed


def _parse_week_of(raw):
    raw = raw or ""
    if raw:
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise BadRequest("weekOf must be in YYYY-MM-DD format")
    return raw


def _meal_out(item):
    return {
        "mealId": item["SK"],
        "name": item["name"],
        "description": item.get("description", ""),
        "priceCents": int(item["priceCents"]),
        "available": bool(item.get("available", True)),
        "macros": {field: int(item.get("macros", {}).get(field, 0)) for field in MACRO_FIELDS},
        "addOnDetails": {
            addon_id: {
                "name": detail.get("name", ""),
                # null when the meal hasn't saved its own price - the add-on's applies.
                "priceCents": int(detail["priceCents"]) if "priceCents" in detail else None,
                "description": detail.get("description", ""),
                "macros": {field: int(detail.get("macros", {}).get(field, 0)) for field in MACRO_FIELDS},
            }
            for addon_id, detail in (item.get("addOnDetails") or {}).items()
        },
        "staple": bool(item.get("staple", False)),
        "weekOf": item.get("weekOf", ""),
    }


# ---------- add-ons ----------
# A single global list, e.g. "Large" (+$3.00), "Large with Cauliflower Rice",
# offered on every meal. A customer picks at most one per meal (combinations
# are add-ons of their own). The add-on holds the name, price and a default
# description; each meal holds its own description and macros for it (see
# _parse_addon_details).

def list_addons():
    items = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": "ADDON"},
    ).get("Items", [])
    # Alphabetical by name everywhere they're listed (Manage menu, each
    # meal's add-on section, the customer menu).
    addons = sorted((_addon_out(item) for item in items), key=lambda a: a["name"].strip().casefold())
    return _response(200, {"addOns": addons})


def create_addon(event):
    _require_admin(event)
    body = _body(event)
    addon_id = uuid.uuid4().hex[:12]
    item = {
        "PK": "ADDON",
        "SK": addon_id,
        "addOnId": addon_id,
        "name": _addon_name(body.get("name")),
        "description": (body.get("description") or "").strip(),
        "priceCents": int(body["priceCents"]),
        "available": bool(body.get("available", True)),
    }
    table.put_item(Item=item)
    return _response(201, _addon_out(item))


def update_addon(event, addon_id):
    _require_admin(event)
    body = _body(event)
    existing = table.get_item(Key={"PK": "ADDON", "SK": addon_id}).get("Item")
    if not existing:
        return _response(404, {"message": "add-on not found"})

    current = _addon_out(existing)
    updated = {
        **existing,
        "name": _addon_name(body["name"]) if "name" in body else current["name"],
        "description": (body.get("description") or "").strip() if "description" in body else current["description"],
        "priceCents": int(body.get("priceCents", existing["priceCents"])),
        "available": bool(body.get("available", existing.get("available", True))),
    }
    # Macros now live on each meal; drop any an older add-on still carries.
    updated.pop("macros", None)
    table.put_item(Item=updated)
    if updated["name"] != current["name"]:
        _rename_addon_on_meals(addon_id, updated["name"])
    return _response(200, _addon_out(updated))


def _rename_addon_on_meals(addon_id, name):
    # Keeps each meal's saved copy of the name current, so it's right if
    # the add-on is later deleted. Prices are per meal, so they're left alone.
    meals = table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": "MEAL"},
    ).get("Items", [])
    for meal in meals:
        if addon_id in (meal.get("addOnDetails") or {}):
            table.update_item(
                Key={"PK": "MEAL", "SK": meal["SK"]},
                UpdateExpression="SET addOnDetails.#id.#name = :name",
                ExpressionAttributeNames={"#id": addon_id, "#name": "name"},
                ExpressionAttributeValues={":name": name},
            )


def delete_addon(event, addon_id):
    _require_admin(event)
    table.delete_item(Key={"PK": "ADDON", "SK": addon_id})
    return _response(204, None)


def _addon_name(raw):
    name = (raw or "").strip()
    if not name:
        raise BadRequest("Add-on name is required")
    return name


def _addon_out(item):
    # Add-ons saved before the name/description split kept their short name
    # (e.g. "Large") in "description" - read that as the name, with no
    # description yet.
    if "name" in item:
        name, description = item["name"], item.get("description", "")
    else:
        name, description = item["description"], ""
    return {
        "addOnId": item["SK"],
        "name": name,
        "description": description,
        "priceCents": int(item["priceCents"]),
        "available": bool(item.get("available", True)),
    }


# ---------- extras & sauces ----------
# Condiments and other extras sold on their own, by quantity (e.g. 2 Salsa
# Verdes), rather than as an option on a meal like an add-on. Each can have
# an optional description and macros (per one), shown wherever it's listed.

def list_extras():
    items = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": "EXTRA"},
    ).get("Items", [])
    extras = sorted((_extra_out(item) for item in items), key=lambda e: e["name"].strip().casefold())
    return _response(200, {"extras": extras})


def create_extra(event):
    _require_admin(event)
    body = _body(event)
    extra_id = uuid.uuid4().hex[:12]
    item = {
        "PK": "EXTRA",
        "SK": extra_id,
        "name": _extra_name(body.get("name")),
        "description": (body.get("description") or "").strip(),
        "macros": _parse_macros(body.get("macros")),
        "priceCents": _extra_price(body.get("priceCents")),
        "available": bool(body.get("available", True)),
    }
    table.put_item(Item=item)
    return _response(201, _extra_out(item))


def update_extra(event, extra_id):
    _require_admin(event)
    body = _body(event)
    existing = table.get_item(Key={"PK": "EXTRA", "SK": extra_id}).get("Item")
    if not existing:
        return _response(404, {"message": "extra not found"})
    updated = {
        **existing,
        "name": _extra_name(body["name"]) if "name" in body else existing["name"],
        "description": (body.get("description") or "").strip() if "description" in body else existing.get("description", ""),
        "macros": _parse_macros(body["macros"] if "macros" in body else existing.get("macros")),
        "priceCents": _extra_price(body["priceCents"]) if "priceCents" in body else int(existing["priceCents"]),
        "available": bool(body.get("available", existing.get("available", True))),
    }
    table.put_item(Item=updated)
    return _response(200, _extra_out(updated))


def delete_extra(event, extra_id):
    # Past orders keep their own copy of the name and price.
    _require_admin(event)
    table.delete_item(Key={"PK": "EXTRA", "SK": extra_id})
    return _response(204, None)


def _extra_name(raw):
    name = (raw or "").strip()
    if not name:
        raise BadRequest("Extra name is required")
    return name


def _extra_price(raw):
    try:
        price_cents = int(raw)
    except (TypeError, ValueError):
        raise BadRequest("Extra price is required")
    if price_cents < 0:
        raise BadRequest("Extra price can't be negative")
    return price_cents


def _extra_out(item):
    return {
        "extraId": item["SK"],
        "name": item["name"],
        "description": item.get("description", ""),
        "macros": _parse_macros(item.get("macros")),
        "priceCents": int(item["priceCents"]),
        "available": bool(item.get("available", True)),
    }


def _resolve_extras(requested):
    """Validates the extras on an order ([{extraId, quantity}]) against the
    current list and returns (extras with name, price, description and
    macros snapshotted, total
    in cents). Prices come from the server, never the client. A hidden or
    deleted extra can't be ordered. Quantities of 0 are dropped.
    """
    resolved = []
    total_cents = 0
    seen = set()
    for entry in requested:
        extra_id = entry["extraId"]
        try:
            quantity = int(entry.get("quantity", 1))
        except (TypeError, ValueError):
            raise BadRequest("Extra quantities must be whole numbers")
        if quantity < 0:
            raise BadRequest("Extra quantities can't be negative")
        if quantity == 0:
            continue
        if extra_id in seen:
            raise BadRequest(f"extra listed twice: {extra_id}")
        seen.add(extra_id)
        item = table.get_item(Key={"PK": "EXTRA", "SK": extra_id}).get("Item")
        if not item or not item.get("available", True):
            raise BadRequest("One of the extras you picked is no longer available - please remove it and try again.")
        extra = _extra_out(item)
        total_cents += extra["priceCents"] * quantity
        resolved.append({
            "extraId": extra_id, "name": extra["name"], "priceCents": extra["priceCents"], "quantity": quantity,
            "description": extra["description"], "macros": extra["macros"],
        })
    return resolved, total_cents


# ---------- customer profile ----------
# Saved contact details (name/email/phone/address) so returning customers
# don't have to retype them at checkout every time, plus notification
# preferences used by the weekly-reminder feature below.

PROFILE_TEXT_FIELDS = ("name", "email", "phone", "address")
# Where this customer picks up (e.g. "Elgin") - set by the admin on the
# Customers page only. The customer sees it at checkout but can't change it,
# and it's copied onto each order for the Excel export.
PICKUP_LOCATION_MAX = 200


def get_profile(event):
    claims = _claims(event)
    item = table.get_item(Key={"PK": f"USER#{claims['sub']}", "SK": "PROFILE"}).get("Item")
    return _response(200, _profile_out(item, claims))


def update_profile(event):
    """Partial update: fields left out of the body keep their saved values.

    Checkout saves just name/email/phone/address back after an order -
    replacing the whole item would silently reset that customer's
    preferredContact to email and re-subscribe them if they'd opted out.
    """
    claims = _claims(event)
    body = _body(event)
    key = {"PK": f"USER#{claims['sub']}", "SK": "PROFILE"}
    existing = table.get_item(Key=key).get("Item") or {}

    preferred_contact = body.get("preferredContact", existing.get("preferredContact", "email"))
    if preferred_contact not in CONTACT_METHODS:
        raise BadRequest(f"preferredContact must be one of {CONTACT_METHODS}")

    item = {
        **key,
        **{field: body.get(field, existing.get(field, "")) for field in PROFILE_TEXT_FIELDS},
        "preferredContact": preferred_contact,
        "unsubscribed": bool(body.get("unsubscribed", existing.get("unsubscribed", False))),
        # Admin-only - never taken from the customer's own request.
        "pickupLocation": existing.get("pickupLocation", ""),
    }
    table.put_item(Item=item)
    return _response(200, _profile_out(item, claims))


def _profile_out(item, claims):
    item = item or {}
    return {
        # Name and email fall back to what the customer gave Cognito at
        # sign-up until they save a profile of their own, so checkout is
        # prefilled even for accounts that never have.
        "name": item.get("name") or claims.get("name", ""),
        "email": item.get("email") or claims.get("email", ""),
        "phone": item.get("phone", ""),
        "address": item.get("address", ""),
        "preferredContact": item.get("preferredContact", "email"),
        "unsubscribed": bool(item.get("unsubscribed", False)),
        "pickupLocation": item.get("pickupLocation", ""),  # read-only for the customer
    }


# ---------- customer accounts (admin) ----------
# The authoritative list of "accounts" is Cognito, not DynamoDB - a customer
# who signed up but never saved a profile or placed an order wouldn't appear
# in a DynamoDB-only scan. This lists every Cognito user and merges in their
# profile row (if any) for the contact fields. Admin-only - exposes every
# customer's contact info.

def _admin_usernames():
    """The Admins group is the one account that runs the business, never a
    customer - this keeps it (and only it) out of customer management.
    """
    usernames = set()
    kwargs = {"UserPoolId": USER_POOL_ID, "GroupName": ADMINS_GROUP_NAME}
    while True:
        page = cognito_idp.list_users_in_group(**kwargs)
        usernames.update(user["Username"] for user in page.get("Users", []))
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return usernames


def list_customers(event):
    _require_admin(event)
    admin_usernames = _admin_usernames()
    customers = []
    kwargs = {"UserPoolId": USER_POOL_ID}
    while True:
        page = cognito_idp.list_users(**kwargs)
        customers.extend(
            _customer_out(user) for user in page.get("Users", []) if user["Username"] not in admin_usernames
        )
        token = page.get("PaginationToken")
        if not token:
            break
        kwargs["PaginationToken"] = token
    return _response(200, {"customers": customers})


def update_customer(event, username):
    _require_admin(event)
    # Resolve to the canonical sub BEFORE checking admin-ness: this pool's
    # UsernameAttributes=["email"] means the real Username is an auto-generated
    # UUID (which _resolve_sub returns, since it equals "sub" here) and email
    # is just a login alias Cognito accepts interchangeably in admin_* calls -
    # comparing the raw, possibly-aliased path param against the admin set
    # directly would silently never match.
    sub = _resolve_sub(username)
    if sub in _admin_usernames():
        raise BadRequest("admin accounts aren't managed as customers")
    body = _body(event)
    preferred_contact = body.get("preferredContact", "email")
    if preferred_contact not in CONTACT_METHODS:
        raise BadRequest(f"preferredContact must be one of {CONTACT_METHODS}")
    existing = table.get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"}).get("Item") or {}
    pickup_location = (
        (body.get("pickupLocation") or "").strip()[:PICKUP_LOCATION_MAX] if "pickupLocation" in body
        else existing.get("pickupLocation", "")
    )

    item = {
        "PK": f"USER#{sub}",
        "SK": "PROFILE",
        **{field: body.get(field, "") for field in PROFILE_TEXT_FIELDS},
        "preferredContact": preferred_contact,
        "unsubscribed": bool(body.get("unsubscribed", False)),
        "pickupLocation": pickup_location,
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
        "pickupLocation": item["pickupLocation"],
    })


def delete_customer(event, username):
    """Deletes the Cognito account (revokes login) and the saved profile row.
    Past orders are kept - they're a business record, not part of "the account".
    """
    _require_admin(event)
    # See update_customer for why this resolves to the canonical sub first.
    sub = _resolve_sub(username)
    if sub in _admin_usernames():
        raise BadRequest("admin accounts aren't managed as customers")
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
        "pickupLocation": profile.get("pickupLocation", ""),
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


def _send_text_via_twilio(phone, message):
    """Texts via Twilio rather than AWS SNS - this AWS org's SCP blocks
    sns:Publish/sms-voice:* entirely (see README), and Twilio is a separate
    vendor unaffected by that restriction. Credentials live in Secrets
    Manager, not DynamoDB/the app's own settings, since the Auth Token is a
    real secret (it can send SMS - and rack up charges - on the account),
    never exposed through any API response. Raises on missing config or a
    non-2xx response; the caller's try/except treats that like any other
    per-recipient failure.
    """
    if not TWILIO_SECRET_ARN:
        raise RuntimeError("Twilio isn't configured - no TWILIO_SECRET_ARN")
    secret = json.loads(secretsmanager.get_secret_value(SecretId=TWILIO_SECRET_ARN)["SecretString"])
    account_sid = secret["accountSid"]

    body = urllib.parse.urlencode({"To": phone, "From": secret["fromNumber"], "Body": message}).encode("utf-8")
    credentials = base64.b64encode(f"{account_sid}:{secret['authToken']}".encode()).decode()
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    urllib.request.urlopen(request, timeout=10)


def send_weekly_reminders(event):
    _require_admin(event)
    message = _reminder_message()
    from_email = _from_email()
    reply_to = _reply_to()
    sms_enabled = bool((table.get_item(Key=SETTINGS_KEY).get("Item") or {}).get("smsEnabled", False))

    sent_email = sent_text = skipped_unsubscribed = skipped_sms_disabled = failed = 0
    errors = []

    for profile in _scan_all_profiles():
        if profile.get("unsubscribed"):
            skipped_unsubscribed += 1
            continue

        channel = profile.get("preferredContact", "email")
        try:
            if channel == "text":
                if not sms_enabled:
                    print(f"SMS not sent to {profile.get('PK')}: smsEnabled global setting is off")
                    skipped_sms_disabled += 1
                    continue
                phone = profile.get("phone")
                if not phone:
                    raise ValueError("no phone number on file")
                _send_text_via_twilio(phone, message)
                sent_text += 1
            else:
                email = profile.get("email")
                if not email:
                    raise ValueError("no email on file")
                ses.send_email(
                    Source=from_email,
                    Destination={"ToAddresses": [email]},
                    ReplyToAddresses=reply_to,
                    Message={
                        "Subject": {"Data": REMINDER_SUBJECT},
                        "Body": {"Text": {"Data": _with_unsubscribe_footer(message, email)}},
                    },
                )
                sent_email += 1
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't abort the batch
            failed += 1
            errors.append(f"{profile.get('PK')}: {e}")
            # Logged as an ERROR so the log-errors alarm reports it too.
            print(f"ERROR sending to {profile.get('PK')}: {e!r}")

    return _response(200, {
        "sentEmail": sent_email,
        "sentText": sent_text,
        "skippedUnsubscribed": skipped_unsubscribed,
        "skippedSmsDisabled": skipped_sms_disabled,
        "failed": failed,
        "errors": errors[:10],  # cap so one noisy failure mode doesn't blow up the response
    })


def send_menu_notification(event):
    """Sends an admin-written message (e.g. "Here is our weekly menu: <link
    to menu-flyer.html>") to every subscribed customer, via whichever
    channel (email/text) each one prefers - same dual-channel pattern as
    send_weekly_reminders, just with an admin-composed message instead of
    the fixed weekly one. Used by the "Send this menu to customers" button
    on menu-flyer.html for both the weekly and monthly menu link.
    """
    _require_admin(event)
    body = _body(event)
    subject = body.get("subject", "").strip()
    message = body.get("message", "").strip()
    if not subject:
        raise BadRequest("subject is required")
    if not message:
        raise BadRequest("message is required")

    from_email = _from_email()
    reply_to = _reply_to()
    sms_enabled = bool((table.get_item(Key=SETTINGS_KEY).get("Item") or {}).get("smsEnabled", False))

    sent_email = sent_text = skipped_unsubscribed = skipped_sms_disabled = skipped_no_contact = failed = 0
    errors = []

    for profile in _scan_all_profiles():
        if profile.get("unsubscribed"):
            skipped_unsubscribed += 1
            continue

        channel = profile.get("preferredContact", "email")
        try:
            if channel == "text":
                if not sms_enabled:
                    print(f"SMS not sent to {profile.get('PK')}: smsEnabled global setting is off")
                    skipped_sms_disabled += 1
                    continue
                phone = profile.get("phone")
                if not phone:
                    skipped_no_contact += 1
                    continue
                _send_text_via_twilio(phone, message)
                sent_text += 1
            else:
                email = profile.get("email")
                if not email:
                    skipped_no_contact += 1
                    continue
                ses.send_email(
                    Source=from_email,
                    Destination={"ToAddresses": [email]},
                    ReplyToAddresses=reply_to,
                    Message={
                        "Subject": {"Data": subject},
                        "Body": {"Text": {"Data": _with_unsubscribe_footer(message, email)}},
                    },
                )
                sent_email += 1
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't abort the batch
            failed += 1
            errors.append(f"{profile.get('PK')}: {e}")
            # Logged as an ERROR so the log-errors alarm reports it too.
            print(f"ERROR sending to {profile.get('PK')}: {e!r}")

    return _response(200, {
        "sentEmail": sent_email,
        "sentText": sent_text,
        "skippedUnsubscribed": skipped_unsubscribed,
        "skippedSmsDisabled": skipped_sms_disabled,
        "skippedNoContact": skipped_no_contact,
        "failed": failed,
        "errors": errors[:10],
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


# ---------- unsubscribe ----------
# A public, unauthenticated link added to the bottom of every customer email
# (see _with_unsubscribe_footer) so a customer can opt out without having to
# sign in. The email address alone isn't enough to authorize this - anyone
# who merely knows a customer's address could otherwise unsubscribe them -
# so the link also carries an HMAC token (keyed by a secret only this Lambda
# can read) that proves the link actually came from a message this system
# sent, not a guess.

def _unsubscribe_signing_key():
    return secretsmanager.get_secret_value(SecretId=UNSUBSCRIBE_SECRET_ARN)["SecretString"]


def _unsubscribe_token(email):
    return hmac.new(_unsubscribe_signing_key().encode(), email.encode(), hashlib.sha256).hexdigest()


def _unsubscribe_url(email):
    query = urllib.parse.urlencode({"email": email, "token": _unsubscribe_token(email)})
    return f"{API_URL}/unsubscribe?{query}"


def _with_unsubscribe_footer(message, email):
    if not UNSUBSCRIBE_SECRET_ARN:
        return message
    return f"{message}\n\n---\nDon't want these emails? Unsubscribe: {_unsubscribe_url(email)}"


def _html_response(status_code, body_html):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": (
            "<!doctype html><html><body style=\"font-family:sans-serif;"
            "max-width:480px;margin:80px auto;text-align:center;\">"
            f"<h1>GTX Meals</h1>{body_html}</body></html>"
        ),
    }


def unsubscribe(event):
    params = event.get("queryStringParameters") or {}
    email = params.get("email", "")
    token = params.get("token", "")
    if not email or not token or not hmac.compare_digest(_unsubscribe_token(email), token):
        return _html_response(403, "<p>This unsubscribe link is invalid.</p>")

    found = False
    for profile in _scan_all_profiles():
        if profile.get("email") == email:
            profile["unsubscribed"] = True
            table.put_item(Item=profile)
            found = True

    if found:
        return _html_response(200, "<p>You've been unsubscribed and won't receive any more emails or texts from us.</p>")
    return _html_response(200, "<p>No account was found for that email, but you won't receive any messages from us.</p>")


# ---------- orders ----------

def list_orders(event):
    user_id = _user_id(event)
    items = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": f"USER#{user_id}", ":sk": "ORDER#"},
        ScanIndexForward=False,  # most recent first
    ).get("Items", [])
    settings = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    return _response(200, {
        "orders": [{**_order_out(item), "changeable": _order_changeable(item, settings)} for item in items]
    })


def create_order(event):
    _require_not_admin(event)
    user_id = _user_id(event)
    body = _body(event)
    requested_items = body.get("items") or []
    if not requested_items:
        # Extras & sauces go with a meal order, not on their own.
        return _response(400, {"message": "order must include at least one meal"})

    # The name always comes from the customer's own profile (falling back to
    # their sign-up name) - never from the request, so it can't be changed
    # per order. The phone is per order: prefilled from the profile at
    # checkout, but whatever they enter is stored on this order only.
    claims = _claims(event)
    profile = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}).get("Item") or {}
    customer_name = (profile.get("name") or claims.get("name") or "").strip()
    if not customer_name:
        raise BadRequest("Add your name on My Account before placing an order.")
    # Optional - but if given, it has to be a whole number we can call.
    contact_phone = (body.get("contactPhone") or "").strip()
    if contact_phone and len(re.sub(r"\D", "", contact_phone)) != 10:
        raise BadRequest("Please enter a 10-digit contact phone number, or leave it blank.")

    settings = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    ready_date_str, _, _ = _ready_dates(settings)
    if not _ordering_status(settings)["open"]:
        raise BadRequest(
            "Ordering is currently closed. Our next menu opens for ordering Monday at 11:00 AM."
        )

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

        resolved_addons, addons_total_cents = _resolve_addons(meal, requested.get("selectedAddOnIds") or [])

        unit_price_cents = int(meal["priceCents"]) + addons_total_cents
        total_cents += unit_price_cents * quantity
        line_items.append({
            "mealId": meal_id,
            "name": meal["name"],
            "unitPriceCents": unit_price_cents,
            "quantity": quantity,
            "selectedAddOns": resolved_addons,
            # Freeform, e.g. "hold the cheese" - a prep instruction for this
            # line, not something the server interprets.
            "note": (requested.get("note") or "").strip(),
        })

    extras, extras_total_cents = _resolve_extras(body.get("extras") or [])
    total_cents += extras_total_cents

    order_id = uuid.uuid4().hex[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"ORDER#{created_at}#{order_id}",
        "orderId": order_id,
        "createdAt": created_at,
        "items": line_items,
        # Extras & sauces: [{extraId, name, priceCents, quantity}].
        "extras": extras,
        "totalCents": total_cents,
        "customerName": customer_name,
        "contactPhone": contact_phone,
        # Optional: someone other than the customer collecting the order.
        # Blank means the customer is picking it up themselves.
        "pickupName": (body.get("pickupName") or "").strip(),
        # The customer's pickup location at the time of the order (admin-set).
        "pickupLocation": profile.get("pickupLocation", ""),
        # Optional feedback for future menus - shown in the Excel export.
        "mealSuggestions": (body.get("mealSuggestions") or "").strip()[:MEAL_SUGGESTIONS_MAX],
        "readyDate": ready_date_str,
        "status": "placed",  # stub checkout - no real payment is taken
        "payment": "unpaid",
        "amountPaidCents": 0,
    }
    # Edit on My Orders cancels the old order and places it again from the
    # menu with replacesOrderId - one "updated" email rather than a cancel
    # and a new order. A payment already recorded carries over to it.
    replaced = _find_order(user_id, str(body["replacesOrderId"])) if body.get("replacesOrderId") else None
    edited = bool(replaced) and replaced.get("status") == "cancelled"
    if edited:
        item["payment"] = replaced.get("payment", "unpaid")
        item["amountPaidCents"] = int(replaced.get("amountPaidCents", 0) or 0)
    table.put_item(Item=item)
    _notify_admin_of_order(item, "updated" if edited else "placed")
    _notify_customer_of_order(item, "updated" if edited else "placed", user_id, claims.get("email", ""))
    if edited:
        _notify_admin_of_refund(item, "updated")
    return _response(201, _order_out(item))


def cancel_order(event, order_id):
    """Self-service cancel, used by the Cancel and Edit buttons on a placed
    order. Edit cancels the order then sends the customer back to the menu
    with the same items reloaded into the cart, rather than mutating a placed
    order in place. Refused after the order's change deadline (see
    _order_changeable).
    """
    user_id = _user_id(event)
    item = _find_order(user_id, order_id)
    if not item:
        raise BadRequest(f"no such order: {order_id}")
    if item.get("status", "placed") != "placed":
        raise BadRequest("only orders in placed status can be cancelled")
    settings = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    if not _order_changeable(item, settings):
        raise BadRequest(
            "This order can no longer be changed or cancelled online - the deadline was Friday at 2:00 PM. "
            "Please email us."
        )
    item["status"] = "cancelled"
    table.put_item(Item=item)
    # Edit cancels too, then places the order again ("updated" email then).
    if not _body(event).get("forEdit"):
        _notify_admin_of_order(item, "cancelled")
        _notify_customer_of_order(item, "cancelled", user_id, _claims(event).get("email", ""))
        _notify_admin_of_refund(item, "cancelled")
    return _response(200, _order_out(item))


# ---------- messages to customers ----------
# Order messages (confirmation, updated, cancelled) go out as soon as the
# change is saved - always by email, plus the short text version for
# customers who prefer text (once texting is switched on). They're about the
# customer's own order, so they ignore the unsubscribe flag and carry no
# unsubscribe link. The two scheduled reminders (see run_job) go by each
# customer's preferred channel, falling back to email while texting is off.
# The wording is the business's own - edit it here.

SIGNATURE = "Christian & Liz\nThe Gym TX Meals"
# The default pickup - used for any customer without a pickup location of
# their own (set by the admin on the Customers page, e.g. "Elgin").
PICKUP_PLACE = "The Gym TX\n4112 Williams Drive, Suite 105\nGeorgetown, TX 78628"
PICKUP_HOURS = "8:00 AM–1:00 PM or 3:30–6:30 PM"
DEFAULT_PICKUP_LOCATION = "The Gym TX, Georgetown"  # same as frontend/js/pricing.js
CUSTOM_PICKUP_NOTE = "Please follow your confirmed pickup instructions."
VENMO = "@lgraffeo88"
CONTACT_EMAIL = "gtxmeals@gmail.com"
WEBSITE = "www.gtxmeals.com"

ORDER_CONFIRMATION_SUBJECT = "Thank You for Your Order! | The Gym TX Meals"
ORDER_UPDATED_SUBJECT = "Your Order Has Been Updated! | The Gym TX Meals"
ORDER_CANCELLED_SUBJECT = "Your Order Has Been Canceled | The Gym TX Meals"
ORDERING_REMINDER_SUBJECT = "Don't Forget to Order Your Meals! | The Gym TX Meals"
PICKUP_REMINDER_SUBJECT = "Your Meals Are Ready for Pickup! | The Gym TX Meals"


def _first_name(name):
    return (name or "").split()[0] if (name or "").strip() else "there"


def _pickup_day(ready_date):
    """"Monday, October 26" - the weekday comes from the date itself, so an
    unusual week's Tuesday pickup reads as Tuesday."""
    if not ready_date:
        return "Monday"
    day = datetime.strptime(ready_date, "%Y-%m-%d")
    return f"{day.strftime('%A, %B')} {day.day}"


def _order_lines(order, with_prices=True):
    def money(cents):
        return f"${int(cents) / 100:.2f}"

    lines = []
    for line in order.get("items") or []:
        quantity = int(line["quantity"])
        addons = ", ".join(a.get("name") or a.get("description", "") for a in line.get("selectedAddOns") or [])
        text = f"{quantity} x {line['name']}" + (f" ({addons})" if addons else "")
        if with_prices:
            text += f" - {money(int(line['unitPriceCents']) * quantity)}"
        lines.append(text)
        if line.get("note"):
            lines.append(f"    Note: {line['note']}")
    for extra in order.get("extras") or []:
        quantity = int(extra["quantity"])
        text = f"{quantity} x {extra['name']}"
        if with_prices:
            text += f" - {money(int(extra['priceCents']) * quantity)}"
        lines.append(text)
    return "\n".join(lines)


def _pickup_location_name(location):
    return (location or "").strip() or DEFAULT_PICKUP_LOCATION


def _pickup_block(location):
    """Where (and when) to pick up: the Georgetown address and hours by
    default, or the customer's own pickup location."""
    if (location or "").strip():
        return f"Pickup Location:\n{location.strip()}\n\n{CUSTOM_PICKUP_NOTE}"
    return f"Pickup Location:\n{PICKUP_PLACE}\n\nPickup Hours:\n{PICKUP_HOURS}"


def _total(order):
    return f"${int(order.get('totalCents', 0)) / 100:.2f}"


def _order_message(order, action):
    """(subject, email body, text body) for an order that was just placed,
    updated (re-placed through Edit) or cancelled."""
    hi = f"Hi {_first_name(order.get('customerName'))}!"
    day = _pickup_day(order.get("readyDate", ""))
    if action == "placed":
        email = (
            f"{hi}\n\n"
            "Thank you for ordering from The Gym TX Meals! We're excited to prepare your meals for the week.\n\n"
            f"Your Order:\n{_order_lines(order)}\n\n"
            f"Order Total: {_total(order)}\n"
            f"Pickup Date: {day}\n\n"
            f"{_pickup_block(order.get('pickupLocation'))}\n\n"
            "Payment Reminder:\n"
            f"Payment is due when ordering. Cash is preferred, or you can send payment through Venmo to {VENMO}.\n"
            "Please note that orders aren't confirmed until payment is received. Unpaid orders without prior "
            "arrangements may be canceled Saturday morning.\n\n"
            "Thank you for supporting our small business!\n"
            f"{SIGNATURE}"
        )
        text = (
            f"{hi} Thanks for ordering from The Gym TX Meals! Your order total is {_total(order)}. "
            f"Payment is due when ordering (Venmo: {VENMO} or cash by arrangement). Pickup is {day} at "
            f"{_pickup_location_name(order.get('pickupLocation'))}. "
            "See your confirmation email for details!"
        )
        return ORDER_CONFIRMATION_SUBJECT, email, text
    if action == "updated":
        email = (
            f"{hi}\n\n"
            "Your order with The Gym TX Meals has been successfully updated!\n\n"
            f"Your Updated Order:\n{_order_lines(order)}\n\n"
            f"Updated Total: {_total(order)}\n"
            f"Pickup Date: {day}\n"
            f"Pickup Location: {_pickup_location_name(order.get('pickupLocation'))}\n\n"
            f"{_updated_payment_note(order)}\n\n"
            "Thank you for ordering from The Gym TX Meals!\n"
            f"Christian & Liz\n{WEBSITE}"
        )
        text = (
            f"{hi} Your Gym TX Meals order has been successfully updated! Your new total is {_total(order)}. "
            f"{_updated_payment_text(order)}Check your email for your updated order details. Thank you!"
        )
        return ORDER_UPDATED_SUBJECT, email, text
    email = (
        f"{hi}\n\n"
        "Your order with The Gym TX Meals has been successfully canceled.\n\n"
        f"Canceled Order:\n{_order_lines(order, with_prices=False)}\n\n"
        f"Order Total: {_total(order)}\n\n"
        f"{_cancelled_payment_note(order)}\n\n"
        "We're sorry we won't be preparing meals for you this week, but we hope to see you again soon!\n"
        f"{SIGNATURE}\n{WEBSITE}"
    )
    text = (
        f"{hi} Your Gym TX Meals order for {day}, has been canceled. "
        + (f"We'll refund your {_money(_amount_paid(order))} payment. " if _amount_paid(order)
           else f"If you've already paid, please email {CONTACT_EMAIL}. ")
        + "We hope to see you again soon!"
    )
    return ORDER_CANCELLED_SUBJECT, email, text


def _money(cents):
    return f"${int(cents) / 100:.2f}"


def _updated_payment_note(order):
    """After an edit: what's still owed or will be refunded, from what the
    admin recorded as paid on the order it replaced."""
    paid, balance = _amount_paid(order), _balance_cents(order)
    if order.get("payment") == "free":
        return "There's nothing to pay for this order."
    if not paid:
        return (f"Payment is due when ordering. Cash is preferred, or you can send payment through Venmo to "
                f"{VENMO}.")
    if balance > 0:
        return (f"You've paid {_money(paid)}, so please send the remaining {_money(balance)} - cash, or Venmo "
                f"to {VENMO}.")
    if balance < 0:
        return f"You've paid {_money(paid)}, so we'll refund the {_money(-balance)} difference."
    return f"You've paid {_money(paid)}, which covers your updated order."


def _updated_payment_text(order):
    paid, balance = _amount_paid(order), _balance_cents(order)
    if paid and balance > 0:
        return f"Please send the remaining {_money(balance)} (Venmo {VENMO} or cash). "
    if paid and balance < 0:
        return f"We'll refund the {_money(-balance)} difference. "
    return ""


def _cancelled_payment_note(order):
    paid = _amount_paid(order)
    if paid:
        return f"We'll refund your {_money(paid)} payment. Questions? Email us at {CONTACT_EMAIL}."
    return f"If you've already submitted payment, please contact us at {CONTACT_EMAIL} so we can review it."


def _ordering_reminder_message(name):
    hi = f"Hi {_first_name(name)}!"
    email = (
        f"{hi}\n\n"
        "Just a friendly reminder that our weekly meal prep orders close tomorrow (Friday) at 2:00 PM, "
        "or sooner if we reach capacity!\n\n"
        "Take a look at this week's menu and place your order before it's too late.\n\n"
        f"Order here: {WEBSITE}\n\n"
        "Thank you for supporting The Gym TX Meals!\n"
        "Christian & Liz"
    )
    text = (
        f"{hi} Don't forget to order your meals for next week! Orders close tomorrow at 2 PM or when spots "
        f"fill. Order at {WEBSITE}"
    )
    return ORDERING_REMINDER_SUBJECT, email, text


def _pickup_reminder_message(name, location=""):
    first = _first_name(name)
    email = (
        f"Good morning, {first}!\n\n"
        "Your meals are ready for pickup today! 🎉\n\n"
        f"{_pickup_block(location)}\n\n"
        "Thank you for ordering! We hope you enjoy your meals this week.\n"
        f"{SIGNATURE}"
    )
    text = (
        f"Good morning, {first}! Your Gym TX Meals are ready for pickup today! Georgetown pickup: "
        "8 AM–1 PM or 3:30–6:30 PM at The Gym TX. See you soon!"
    ) if not (location or "").strip() else (
        f"Good morning, {first}! Your Gym TX Meals are ready for pickup today at {location.strip()}. "
        "See you soon!"
    )
    return PICKUP_REMINDER_SUBJECT, email, text


def _sms_enabled():
    return bool((table.get_item(Key=SETTINGS_KEY).get("Item") or {}).get("smsEnabled", False))


def _customer_contact(user_id, fallback_email=""):
    """The customer's profile, with their login email if the profile has none."""
    profile = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}).get("Item") or {}
    email = profile.get("email") or fallback_email
    if not email:
        try:
            user = cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=user_id)
            email = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}.get("email", "")
        except Exception as e:  # noqa: BLE001 - no email just means no email message
            print(f"ERROR looking up the email for {user_id}: {e!r}")
    return {**profile, "email": email}


def _send_email(to, subject, body):
    ses.send_email(
        Source=_from_email(),
        Destination={"ToAddresses": [to]},
        ReplyToAddresses=_reply_to(),
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )


def _wants_text(contact, sms_enabled):
    return contact.get("preferredContact") == "text" and sms_enabled and bool(contact.get("phone"))


def _notify_customer_of_order(order, action, user_id, fallback_email=""):
    """Best effort, after the order change is saved: a failure is logged as
    an ERROR (the log-errors alarm reports it) and never undoes the change."""
    if not FROM_EMAIL:
        return  # no sending domain configured (e.g. local runs)
    try:
        contact = _customer_contact(user_id, fallback_email)
        subject, email_body, text_body = _order_message(order, action)
        if contact.get("email"):
            _send_email(contact["email"], subject, email_body)
        if _wants_text(contact, _sms_enabled()):
            _send_text_via_twilio(contact["phone"], text_body)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR sending the {action} message for order {order.get('orderId')}: {e!r}")


def _send_reminder(contact, message, sms_enabled):
    """A scheduled reminder by the customer's preferred channel - email
    (with an unsubscribe link) when they prefer it, or while texting is off."""
    subject, email_body, text_body = message
    if _wants_text(contact, sms_enabled):
        _send_text_via_twilio(contact["phone"], text_body)
    elif contact.get("email"):
        _send_email(contact["email"], subject, _with_unsubscribe_footer(email_body, contact["email"]))
    else:
        raise ValueError("no email on file")


# ---------- scheduled jobs ----------
# EventBridge invokes this Lambda directly with {"job": ...} (see the stack).
# Each rule fires at both possible UTC hours, since Central time shifts
# with daylight saving; the job only runs in the right Central hour.
#   ordering-reminder: Thursday 6 PM - customers who haven't ordered yet.
#   pickup-reminder:   7 AM on the pickup day - customers with an order.
#   unpaid-orders-report: Saturday 8 AM - the admin, a list of unpaid orders.
# Every send is recorded first (SENT#<job>#<date>, one item per customer),
# so a retried run never messages anyone twice.

ORDERING_REMINDER_AT = (3, 18)  # Thursday, 6 PM Central
PICKUP_REMINDER_HOUR = 7  # 7 AM Central on the ready date
UNPAID_REPORT_AT = (5, 8)  # Saturday, 8 AM Central
ACTIVE_ORDER_STATUSES = ("placed", "prepared", "delivered")


def run_job(job, context=None):
    local = _central(_utcnow())
    if job == "ordering-reminder":
        if (local.weekday(), local.hour) != ORDERING_REMINDER_AT:
            return {"skipped": "not Thursday 6 PM Central"}
        result = send_ordering_reminders(local, context)
    elif job == "pickup-reminder":
        if local.hour != PICKUP_REMINDER_HOUR:
            return {"skipped": "not 7 AM Central"}
        result = send_pickup_reminders(local, context)
    elif job == "unpaid-orders-report":
        if (local.weekday(), local.hour) != UNPAID_REPORT_AT:
            return {"skipped": "not Saturday 8 AM Central"}
        result = send_unpaid_orders_report(local)
    else:
        raise ValueError(f"unknown job: {job}")
    print(f"{job}: {json.dumps(result)}")
    return result


def send_unpaid_orders_report(local):
    """Saturday morning, after ordering closes: one email to the admin
    listing every order for the coming pickup that's unpaid or only partly
    paid - to follow up on. Nothing is cancelled automatically. Sent once
    per pickup date (a retried run doesn't send it again); when every order
    is paid it says so, so it's clear the check ran."""
    if not FROM_EMAIL:
        return {"skipped": "no sending domain configured"}
    settings = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    ready_date = _ready_dates(settings, local.astimezone(timezone.utc))[0]
    orders = [
        o for o in _orders_for_ready_date(ready_date)
        if o.get("status", "placed") in ("placed", "prepared")
    ]
    owing = []
    for order in orders:
        # Free orders owe nothing (_balance_cents is 0 for them).
        due = int(order.get("totalCents", 0)) if order.get("payment", "unpaid") == "unpaid" else _balance_cents(order)
        if due > 0:
            owing.append((order, due))
    owing.sort(key=lambda pair: (pair[0].get("customerName") or "").casefold())
    if not _claim_send(f"unpaid-orders-report#{ready_date}", "admin"):
        return {"skipped": "already sent for this pickup", "readyDate": ready_date}

    day = _pickup_day(ready_date)
    total_due = sum(due for _, due in owing)
    if owing:
        subject = f"Unpaid Orders for {day}: {len(owing)} ({_money(total_due)} due)"
        lines = [f"{len(owing)} of {len(orders)} orders for {day} aren't fully paid:", ""]
        for order, due in owing:
            paid = _amount_paid(order)
            line = f"- {order.get('customerName') or 'A customer'}: {_money(due)} due"
            line += f" (paid {_money(paid)} of {_money(order.get('totalCents', 0))})" if paid else \
                f" (total {_money(order.get('totalCents', 0))}, nothing recorded)"
            if order.get("contactPhone"):
                line += f" - {order['contactPhone']}"
            lines.append(line)
        lines += ["", f"Total due: {_money(total_due)}", "",
                  "Nothing has been cancelled. Record payments, or cancel orders, on the Orders page "
                  "(Payment: Unpaid only)."]
    else:
        subject = f"All Orders Paid for {day}"
        lines = [f"All {len(orders)} orders for {day} are paid." if orders else f"There are no orders for {day}."]
    if SITE_URL:
        lines += ["", f"{SITE_URL.rstrip('/')}/admin-orders.html"]
    try:
        to = _admin_emails()
        if to:
            ses.send_email(
                Source=FROM_EMAIL,
                Destination={"ToAddresses": to},
                Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": "\n".join(lines)}}},
            )
    except Exception as e:  # noqa: BLE001
        # Un-record it so Lambda's retry tries again.
        table.delete_item(Key={"PK": f"SENT#unpaid-orders-report#{ready_date}", "SK": "admin"})
        print(f"ERROR sending the unpaid orders report for {ready_date}: {e!r}")
        raise
    return {"readyDate": ready_date, "unpaid": len(owing), "dueCents": total_due}


def _orders_for_ready_date(ready_date):
    items = []
    scan_kwargs = {
        "FilterExpression": "begins_with(SK, :sk) AND readyDate = :d",
        "ExpressionAttributeValues": {":sk": "ORDER#", ":d": ready_date},
    }
    while True:
        page = table.scan(**scan_kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _claim_send(job_key, user_id):
    """Records that this customer is being sent this job's message - False
    if they already were (a retried run)."""
    try:
        table.put_item(
            Item={"PK": f"SENT#{job_key}", "SK": user_id, "sentAt": _utcnow().isoformat()},
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _send_job_messages(job_key, recipients, context):
    """recipients: [(user_id, contact, message)]. A failed send is logged and
    un-recorded, so a retry tries that customer again."""
    sms_enabled = _sms_enabled()
    sent = skipped = failed = 0
    for user_id, contact, message in recipients:
        # Stop short of the Lambda timeout; raising makes Lambda retry the
        # run, which picks up with whoever hasn't been sent yet.
        if context is not None and context.get_remaining_time_in_millis() < 2000:
            raise RuntimeError(f"{job_key}: ran out of time after {sent} sent; the retry continues")
        if not _claim_send(job_key, user_id):
            skipped += 1
            continue
        try:
            _send_reminder(contact, message, sms_enabled)
            sent += 1
        except Exception as e:  # noqa: BLE001 - one bad recipient shouldn't stop the rest
            table.delete_item(Key={"PK": f"SENT#{job_key}", "SK": user_id})
            failed += 1
            print(f"ERROR sending {job_key} to USER#{user_id}: {e!r}")
    return {"sent": sent, "alreadySent": skipped, "failed": failed}


def send_ordering_reminders(local, context=None):
    """Thursday evening: every opted-in customer (not unsubscribed) who
    hasn't ordered for this week's pickup yet. Skipped when ordering is
    closed, e.g. a holiday week."""
    settings = table.get_item(Key=SETTINGS_KEY).get("Item") or {}
    if not _ordering_status(settings)["open"]:
        return {"skipped": "ordering is closed"}
    ready_date = _ready_dates(settings)[0]
    ordered = {
        o["PK"].split("#", 1)[1]
        for o in _orders_for_ready_date(ready_date)
        if o.get("status", "placed") in ACTIVE_ORDER_STATUSES
    }
    admins = _admin_usernames()
    recipients = []
    for profile in _scan_all_profiles():
        user_id = profile["PK"].split("#", 1)[1]
        if profile.get("unsubscribed") or user_id in ordered or user_id in admins:
            continue
        contact = _customer_contact(user_id)
        recipients.append((user_id, contact, _ordering_reminder_message(contact.get("name"))))
    return _send_job_messages(f"ordering-reminder#{ready_date}", recipients, context)


def send_pickup_reminders(local, context=None):
    """Pickup morning: one message to each customer with an order (placed
    or prepared) for today. Nothing is sent on a day with no pickups."""
    today = local.date().isoformat()
    names = {}
    for order in _orders_for_ready_date(today):
        if order.get("status", "placed") in ("placed", "prepared"):
            names.setdefault(order["PK"].split("#", 1)[1], order.get("customerName", ""))
    recipients = []
    for user_id, name in names.items():
        contact = _customer_contact(user_id)
        # The customer's pickup location as it's stored now (the admin may
        # have set it after the order was placed).
        recipients.append((user_id, contact, _pickup_reminder_message(
            contact.get("name") or name, contact.get("pickupLocation", ""),
        )))
    return _send_job_messages(f"pickup-reminder#{today}", recipients, context)


# ---------- order notifications to the admin ----------
# A short email to the admin (the Admins-group account's address) whenever a
# customer places, edits or cancels an order. Best effort: a failure is
# logged as an ERROR (so the log-errors alarm reports it) but never fails
# the customer's order.

ORDER_EMAIL_SUBJECTS = {
    "placed": "New Meal Order Received!",
    "updated": "Meal Order Updated",
    "cancelled": "Meal Order Cancelled",
}
ORDER_EMAIL_VERBS = {"placed": "placed an order", "updated": "updated their order", "cancelled": "cancelled their order"}


def _notify_admin_of_order(order, action):
    if not FROM_EMAIL:
        return  # no sending domain configured (e.g. local runs)
    try:
        to = _admin_emails()
        if not to:
            return
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": to},
            Message={
                "Subject": {"Data": ORDER_EMAIL_SUBJECTS[action]},
                "Body": {"Text": {"Data": _order_email_text(order, action)}},
            },
        )
    except Exception as e:  # noqa: BLE001 - the order itself already went through
        print(f"ERROR emailing the admin about order {order.get('orderId')} ({action}): {e!r}")


def _notify_admin_of_refund(order, action):
    """A separate "Refund Needed" email to the admin when an edit lowered a
    paid order's total, or a paid order was cancelled - so it isn't missed
    in the usual order email. Best effort, like the others."""
    refund = -_balance_cents(order)
    if refund <= 0 or not FROM_EMAIL:
        return
    name = order.get("customerName") or "A customer"
    why = "their order was cancelled" if action == "cancelled" else "they changed their order"
    method = "Venmo" if order.get("payment") == "venmo" else "cash"
    lines = [
        f"Please refund {name} {_money(refund)}: {why}.",
        "",
        f"Paid: {_money(_amount_paid(order))} ({method})",
        f"Order total now: {_money(0 if order.get('status') == 'cancelled' else order.get('totalCents', 0))}",
        f"Refund: {_money(refund)}",
    ]
    if order.get("contactPhone"):
        lines.append(f"Phone: {order['contactPhone']}")
    lines += ["", "After refunding, update the amount paid on the Orders page."]
    if SITE_URL:
        lines.append(f"{SITE_URL.rstrip('/')}/admin-orders.html")
    try:
        to = _admin_emails()
        if not to:
            return
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": to},
            Message={
                "Subject": {"Data": f"Refund Needed: {_money(refund)} for {name}"},
                "Body": {"Text": {"Data": "\n".join(lines)}},
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR emailing the admin about a refund for order {order.get('orderId')}: {e!r}")


def _order_email_text(order, action):
    def money(cents):
        return f"${int(cents) / 100:.2f}"

    name = order.get("customerName") or "A customer"
    ready = order.get("readyDate", "")
    when = ""
    if ready:
        day = datetime.strptime(ready, "%Y-%m-%d")
        when = f" for {day.strftime('%A, %B')} {day.day}"
    lines = [f"{name} {ORDER_EMAIL_VERBS[action]}{when}.", ""]
    for line in order.get("items") or []:
        addons = ", ".join(a.get("name") or a.get("description", "") for a in line.get("selectedAddOns") or [])
        text = f"{int(line['quantity'])} x {line['name']}"
        if addons:
            text += f" ({addons})"
        if line.get("note"):
            text += f" - note: {line['note']}"
        lines.append(text)
    for extra in order.get("extras") or []:
        lines.append(f"{int(extra['quantity'])} x {extra['name']}")
    lines += ["", f"Total: {money(order.get('totalCents', 0))}"]
    if _amount_paid(order):
        balance = _balance_cents(order)
        lines.append(f"Paid: {money(_amount_paid(order))}"
                     + (f" - balance due {money(balance)}" if balance > 0 else "")
                     + (f" - refund {money(-balance)}" if balance < 0 else ""))
    if order.get("contactPhone"):
        lines.append(f"Phone: {order['contactPhone']}")
    lines.append(f"Pickup location: {_pickup_location_name(order.get('pickupLocation'))}")
    if order.get("pickupName"):
        lines.append(f"Picked up by: {order['pickupName']}")
    if order.get("mealSuggestions"):
        lines.append(f"Meal suggestions: {order['mealSuggestions']}")
    if SITE_URL:
        lines += ["", f"All orders: {SITE_URL.rstrip('/')}/admin-orders.html"]
    return "\n".join(lines)


def list_admin_orders(event):
    _require_admin(event)
    status = (event.get("queryStringParameters") or {}).get("status", "placed")
    if status != "all" and status not in ORDER_STATUSES:
        raise BadRequest(f"status must be \"all\" or one of {ORDER_STATUSES}")

    items = []
    if status == "all":
        scan_kwargs = {
            "FilterExpression": "begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":sk": "ORDER#"},
        }
    else:
        scan_kwargs = {
            "FilterExpression": "begins_with(SK, :sk) AND #s = :status",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":sk": "ORDER#", ":status": status},
        }
    while True:
        page = table.scan(**scan_kwargs)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    orders = [{**_order_out(item), "customerSub": item["PK"].split("#", 1)[1]} for item in items]
    orders.sort(key=lambda o: o["createdAt"])
    return _response(200, {"orders": orders})


def update_order_status(event, sub, order_id):
    """The admin changing an order's status and/or payment - either or both
    may be sent."""
    _require_admin(event)
    body = _body(event)
    if not {"status", "payment", "amountPaidCents"} & set(body):
        raise BadRequest("send a status, payment or amountPaidCents")
    status = body.get("status")
    if "status" in body and status not in ORDER_STATUSES:
        raise BadRequest(f"status must be one of {ORDER_STATUSES}")
    if "payment" in body and body["payment"] not in PAYMENT_STATUSES:
        raise BadRequest(f"payment must be one of {PAYMENT_STATUSES}")
    item = _find_order(sub, order_id)
    if not item:
        raise BadRequest(f"no such order: {order_id}")
    was = item.get("status", "placed")
    if "status" in body:
        item["status"] = status
    if "payment" in body:
        item["payment"] = body["payment"]
        if "amountPaidCents" not in body:
            # Picking Venmo or cash starts the amount at the order total (the
            # usual case); unpaid or free clears it.
            if body["payment"] in PAID_METHODS:
                if not int(item.get("amountPaidCents", 0) or 0):
                    item["amountPaidCents"] = int(item["totalCents"])
            else:
                item["amountPaidCents"] = 0
    if "amountPaidCents" in body:
        try:
            amount = int(body["amountPaidCents"])
        except (TypeError, ValueError):
            raise BadRequest("amountPaidCents must be a whole number of cents")
        if not 0 <= amount <= MAX_AMOUNT_PAID_CENTS:
            raise BadRequest("amount paid must be between $0 and $100,000")
        item["amountPaidCents"] = amount
    table.put_item(Item=item)
    # The admin cancelling (e.g. unpaid by Saturday): the customer gets the
    # same cancellation email as when they cancel it themselves.
    if status == "cancelled" and was != "cancelled":
        _notify_customer_of_order(item, "cancelled", sub)
        _notify_admin_of_refund(item, "cancelled")
    return _response(200, {**_order_out(item), "customerSub": sub})


def _find_order(user_id, order_id):
    items = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": f"USER#{user_id}", ":sk": "ORDER#"},
    ).get("Items", [])
    for item in items:
        if item["orderId"] == order_id:
            return item
    return None


def _admin_emails():
    emails = []
    kwargs = {"UserPoolId": USER_POOL_ID, "GroupName": ADMINS_GROUP_NAME}
    while True:
        page = cognito_idp.list_users_in_group(**kwargs)
        for user in page.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
            if attrs.get("email"):
                emails.append(attrs["email"])
        token = page.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    return emails


def _from_email():
    """The sender for customer emails (weekly reminders, menu
    notifications): a fixed address on the site's SES-verified domain, so
    messages are DKIM-signed by that domain and pass DMARC - see README.
    """
    if not FROM_EMAIL:
        raise BadRequest("no sender address is configured (FROM_EMAIL is empty)")
    return FROM_EMAIL


def _reply_to():
    """Reply-To for customer emails: the admin's own address, resolved from
    Cognito rather than a hardcoded setting, so it always matches whoever
    actually holds the Admins-group account. The sending domain has no
    mailbox, so without this, customer replies would bounce. With no admin
    configured the email still sends, just without a Reply-To.
    """
    emails = _admin_emails()
    return emails[:1]


def _resolve_addons(meal, requested_addon_ids):
    """Validates the client's add-on pick (at most one per meal) against the
    current global add-on list and returns (resolved add-ons with name, this
    meal's price for it and this meal's macros for it snapshotted, total
    price delta in cents). Raises BadRequest for more than one add-on, or an
    id that doesn't currently exist (e.g. a deleted add-on, even though the
    meal still has its copy). Macros are snapshotted (not looked up live) for the same
    reason name/price are - so a past order's label is unaffected by the
    nutrition info changing later.
    """
    if len(requested_addon_ids) > 1:
        raise BadRequest("Choose one add-on per meal.")
    details = meal.get("addOnDetails") or {}
    resolved = []
    total_cents = 0
    for addon_id in requested_addon_ids:
        addon = table.get_item(Key={"PK": "ADDON", "SK": addon_id}).get("Item")
        if not addon:
            raise BadRequest(f"unknown add-on: {addon_id}")
        detail = details.get(addon_id) or {}
        # Same rule as the menu (isAddOnOffered in pricing.js): only an
        # add-on the meal has a description or macros for is offered on it.
        if not addon.get("available", True) or not _addon_offered(detail):
            raise BadRequest(f"{_addon_out(addon)['name']} isn't offered on {meal['name']}.")
        price_cents = int(detail["priceCents"]) if "priceCents" in detail else int(addon["priceCents"])
        total_cents += price_cents
        resolved.append({
            "addOnId": addon_id,
            "name": _addon_out(addon)["name"],
            "priceCents": price_cents,
            "macros": _parse_macros(detail.get("macros")),
        })
    return resolved, total_cents


def _addon_offered(detail):
    return bool((detail.get("description") or "").strip()) or any(
        int(v or 0) for v in (detail.get("macros") or {}).values()
    )


def _line_item_out(line):
    # Orders placed before add-ons had a separate name snapshotted the
    # add-on's name as "description".
    return {
        **line,
        "selectedAddOns": [
            {k: v for k, v in {**a, "name": a.get("name") or a.get("description", "")}.items() if k != "description"}
            for a in line.get("selectedAddOns") or []
        ],
    }


def _order_out(item):
    return {
        "orderId": item["orderId"],
        "createdAt": item["createdAt"],
        "items": [_line_item_out(i) for i in item["items"]],
        "extras": [
            {**e, "priceCents": int(e["priceCents"]), "quantity": int(e["quantity"]),
             "description": e.get("description", ""), "macros": _parse_macros(e.get("macros"))}
            for e in item.get("extras") or []
        ],
        "totalCents": int(item["totalCents"]),
        # Orders placed before these fields existed stored deliveryName /
        # deliveryPhone instead - fall back so they still show who and how
        # to reach them. (Their old email/address fields are no longer shown.)
        "customerName": item.get("customerName") or item.get("deliveryName", ""),
        "contactPhone": item.get("contactPhone") or item.get("deliveryPhone", ""),
        "pickupName": item.get("pickupName", ""),
        "pickupLocation": item.get("pickupLocation", ""),
        "mealSuggestions": item.get("mealSuggestions", ""),
        "readyDate": item.get("readyDate", ""),
        "status": item.get("status", "placed"),
        "payment": item.get("payment", "unpaid"),
        "amountPaidCents": int(item.get("amountPaidCents", 0) or 0),
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


def _is_admin(claims):
    # Cognito serializes group membership as a stringified list in the JWT claims
    # surfaced by API Gateway, so this checks substring membership rather than
    # parsing it as JSON.
    groups = claims.get("cognito:groups", "")
    return ADMINS_GROUP_NAME in groups


def _require_admin(event):
    if not _is_admin(_claims(event)):
        raise AuthError("admin privileges required", 403)


def _require_not_admin(event):
    """The single Admins-group account manages the business; it never acts as
    a customer. Used to keep the two roles from overlapping (e.g. placing an
    order), not as a security boundary - the admin already has full API access.
    """
    if _is_admin(_claims(event)):
        raise BadRequest("admin accounts can't place orders")


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
