import json
import os
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("TABLE_NAME", "MealPrepTable")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ADMINS_GROUP_NAME", "Admins")
# Real value is set per-test (via _create_test_pool) once a moto-mocked pool
# exists; this placeholder just satisfies the required os.environ[...] read
# at module import time.
os.environ.setdefault("USER_POOL_ID", "placeholder")
# Mirrors the deployed stack's sender on the site's verified domain; tests
# that send email verify example.com in moto's SES (see _verify_sender_domain).
os.environ.setdefault("FROM_EMAIL", "GTX Meals <menu@example.com>")


# Wednesday 2026-10-21 at 12:00 PM CDT - inside the ordering window, so
# tests that place orders don't depend on when they're run. Tests about the
# schedule itself set their own time with _at().
OPEN_TIME = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
OPEN_STATUS = {"open": True, "reason": "schedule"}


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    # Imported under moto (like every test does) - the module creates its
    # boto3 clients at import time.
    with mock_aws():
        from backend.lambda_src import index

    monkeypatch.setattr(index, "_utcnow", lambda: OPEN_TIME)


def _at(monkeypatch, iso_utc):
    from backend.lambda_src import index

    monkeypatch.setattr(index, "_utcnow", lambda: datetime.fromisoformat(iso_utc))


@pytest.fixture
def table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="MealPrepTable",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _event(method, path, body=None, path_params=None, claims=None, query=None):
    event = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
    }
    if path_params:
        event["pathParameters"] = path_params
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    if query:
        event["queryStringParameters"] = query
    return event


# "name" is the sign-up name Cognito puts in the ID token - orders fall
# back to it for the customer's name when they haven't saved a profile.
CUSTOMER_CLAIMS = {"sub": "user-1", "email": "customer@example.com", "name": "Casey Customer"}
# Every order needs a contact phone.
PHONE = "(555) 555-1234"
ADMIN_CLAIMS = {"sub": "admin-1", "email": "admin@example.com", "cognito:groups": "[Admins]"}


def _create_meal(index, name="Chicken Bowl", price_cents=1200):
    resp = index.handler(
        _event("POST", "/meals", {"name": name, "description": "", "priceCents": price_cents}, claims=ADMIN_CLAIMS),
        None,
    )
    return json.loads(resp["body"])


def test_create_meal_with_macros(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event(
            "POST", "/meals",
            {
                "name": "Chicken Teriyaki Bowl",
                "priceCents": 1200,
                "macros": {"calories": 540, "proteinG": 48, "carbsG": 52, "fatG": 14},
            },
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    meal = json.loads(resp["body"])
    assert meal["macros"] == {"calories": 540, "proteinG": 48, "carbsG": 52, "fatG": 14}


def test_create_meal_defaults_macros_when_omitted(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    assert meal["macros"] == {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0}


def test_create_meal_defaults_staple_and_week_of_when_omitted(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    assert meal["staple"] is False
    assert meal["weekOf"] == ""


def test_create_meal_with_staple_and_week_of(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event(
            "POST", "/meals",
            {"name": "Chicken Bowl", "priceCents": 1200, "staple": True, "weekOf": "2026-10-19"},
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    meal = json.loads(resp["body"])
    assert meal["staple"] is True
    assert meal["weekOf"] == "2026-10-19"


def test_create_meal_rejects_invalid_week_of_format(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/meals", {"name": "Chicken Bowl", "priceCents": 1200, "weekOf": "10/19/2026"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_update_meal_merges_staple_and_week_of_instead_of_overwriting(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    index.handler(
        _event("PUT", f"/meals/{meal['mealId']}", {"weekOf": "2026-10-19"}, path_params={"mealId": meal["mealId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    # A later update naming only priceCents must leave weekOf as it was,
    # not reset it back to blank - same merge pattern as settings.
    resp = index.handler(
        _event("PUT", f"/meals/{meal['mealId']}", {"priceCents": 1500}, path_params={"mealId": meal["mealId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    updated = json.loads(resp["body"])
    assert updated["weekOf"] == "2026-10-19"
    assert updated["priceCents"] == 1500


def test_update_meal_rejects_invalid_week_of_format(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("PUT", f"/meals/{meal['mealId']}", {"weekOf": "not-a-date"}, path_params={"mealId": meal["mealId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def _offer_addons(index, meal_id, *addon_ids):
    # An add-on is only offered on a meal with a description or macros for it.
    _put_meal(index, meal_id, {"addOnDetails": {addon_id: {"description": "On this meal"} for addon_id in addon_ids}})


def _create_addon(index, name="Large", price_cents=300, description=""):
    resp = index.handler(
        _event("POST", "/addons", {"name": name, "description": description, "priceCents": price_cents},
               claims=ADMIN_CLAIMS),
        None,
    )
    return json.loads(resp["body"])


def test_list_addons_is_public(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/addons"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"addOns": []}


def test_create_addon_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/addons", {"name": "Large", "priceCents": 300}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 403


def test_addon_has_no_macros_of_its_own(table):
    # Macros are per meal now (the meal's addOnDetails) - anything sent for
    # the add-on itself is ignored, and an older add-on's macros are dropped.
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/addons", {"name": "Cauliflower Rice", "priceCents": 100,
                                   "macros": {"calories": 25}}, claims=ADMIN_CLAIMS),
        None,
    )
    assert "macros" not in json.loads(resp["body"])
    index.table.put_item(Item={"PK": "ADDON", "SK": "old", "addOnId": "old", "name": "Large",
                               "priceCents": 300, "macros": {"calories": 200}})
    updated = json.loads(_put_addon(index, "old", {"priceCents": 350})["body"])
    assert "macros" not in updated
    assert "macros" not in index.table.get_item(Key={"PK": "ADDON", "SK": "old"})["Item"]


def test_admin_can_create_and_delete_addon(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    assert addon["name"] == "Large"
    assert addon["priceCents"] == 300

    list_resp = index.handler(_event("GET", "/addons"), None)
    assert len(json.loads(list_resp["body"])["addOns"]) == 1

    delete_resp = index.handler(
        _event("DELETE", f"/addons/{addon['addOnId']}", path_params={"addOnId": addon["addOnId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert delete_resp["statusCode"] == 204

    list_resp = index.handler(_event("GET", "/addons"), None)
    assert json.loads(list_resp["body"]) == {"addOns": []}


def test_create_addon_defaults_to_available(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    assert addon["available"] is True


def test_update_addon_requires_admin(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    resp = index.handler(
        _event("PUT", f"/addons/{addon['addOnId']}", {"available": False},
               path_params={"addOnId": addon["addOnId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 403


def test_admin_can_hide_and_show_addon(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    resp = index.handler(
        _event("PUT", f"/addons/{addon['addOnId']}", {"available": False},
               path_params={"addOnId": addon["addOnId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 200
    updated = json.loads(resp["body"])
    assert updated["available"] is False
    # Hiding doesn't touch description/price.
    assert updated["name"] == "Large"
    assert updated["priceCents"] == 300

    list_resp = index.handler(_event("GET", "/addons"), None)
    assert json.loads(list_resp["body"])["addOns"][0]["available"] is False

    resp = index.handler(
        _event("PUT", f"/addons/{addon['addOnId']}", {"available": True},
               path_params={"addOnId": addon["addOnId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert json.loads(resp["body"])["available"] is True


def test_update_addon_fails_for_unknown_id(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/addons/does-not-exist", {"available": False},
               path_params={"addOnId": "does-not-exist"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 404


def _settings(**overrides):
    # GET/PUT /settings on the pinned clock (Wednesday 2026-10-21), when the
    # automatic ready date is Monday 2026-10-26.
    return {
        "nextReadyDate": "2026-10-26", "readyDateAuto": "2026-10-26",
        "readyDateOverride": "", "readyDateOverrideUsed": False,
        "smsEnabled": False, "orderingMode": "auto", "ordering": OPEN_STATUS,
        **overrides,
    }


def _put_settings(index, body, claims=ADMIN_CLAIMS):
    return index.handler(_event("PUT", "/settings", body, claims=claims), None)


def test_get_settings_defaults_to_the_automatic_ready_date_and_sms_off(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/settings"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {**_settings(), "contactEmail": ""}


def test_update_settings_requires_admin(table):
    from backend.lambda_src import index

    assert _put_settings(index, {"smsEnabled": True}, claims=CUSTOMER_CLAIMS)["statusCode"] == 403


def test_admin_can_update_settings(table):
    from backend.lambda_src import index

    # Tuesday pickup after a holiday Monday.
    resp = _put_settings(index, {"readyDateOverride": "2026-10-27", "smsEnabled": True})
    assert resp["statusCode"] == 200
    expected = _settings(nextReadyDate="2026-10-27", readyDateOverride="2026-10-27",
                         readyDateOverrideUsed=True, smsEnabled=True)
    assert json.loads(resp["body"]) == expected

    get_resp = index.handler(_event("GET", "/settings"), None)
    assert json.loads(get_resp["body"]) == {**expected, "contactEmail": ""}


def test_get_settings_logs_an_error_when_the_contact_email_lookup_fails(table, monkeypatch, capsys):
    from backend.lambda_src import index

    def fail():
        raise RuntimeError("Cognito is down")

    monkeypatch.setattr(index, "_admin_emails", fail)
    resp = index.handler(_event("GET", "/settings"), None)

    assert json.loads(resp["body"])["contactEmail"] == ""  # still answers
    assert "ERROR looking up the admin contact email" in capsys.readouterr().out


def test_get_settings_reports_the_admin_email_as_contact_email(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")

    resp = index.handler(_event("GET", "/settings"), None)
    assert json.loads(resp["body"])["contactEmail"] == "admin@example.com"


def test_update_settings_defaults_sms_enabled_to_false_when_omitted(table):
    from backend.lambda_src import index

    assert json.loads(_put_settings(index, {"orderingMode": "auto"})["body"])["smsEnabled"] is False


def test_update_settings_merges_instead_of_overwriting(table):
    from backend.lambda_src import index

    _put_settings(index, {"smsEnabled": True})

    # Saving just the ready date (as the Builder page's ready-date form
    # does) must not silently reset smsEnabled back to False.
    body = json.loads(_put_settings(index, {"readyDateOverride": "2026-10-27"})["body"])
    assert (body["readyDateOverride"], body["smsEnabled"]) == ("2026-10-27", True)

    # And saving just smsEnabled must not reset the override either.
    body = json.loads(_put_settings(index, {"smsEnabled": False})["body"])
    assert (body["readyDateOverride"], body["smsEnabled"]) == ("2026-10-27", False)


def test_update_settings_rejects_bad_date_format(table):
    from backend.lambda_src import index

    assert _put_settings(index, {"readyDateOverride": "12/01/2026"})["statusCode"] == 400


# ---------- automatic ready date ----------


@pytest.mark.parametrize("when, ready", [
    ("2026-10-19T15:59:00+00:00", "2026-10-19"),  # Monday 10:59 AM - today's pickup, ordering closed
    ("2026-10-19T16:00:00+00:00", "2026-10-26"),  # Monday 11:00 AM - ordering opens for next Monday
    ("2026-10-23T21:30:00+00:00", "2026-10-26"),  # Friday after closing
    ("2026-10-25T17:00:00+00:00", "2026-10-26"),  # Sunday
    ("2026-12-28T16:30:00+00:00", "2026-12-28"),  # Monday 10:30 AM CST
    ("2026-12-28T17:00:00+00:00", "2027-01-04"),  # Monday 11:00 AM CST - across the new year
])
def test_ready_date_follows_the_ordering_schedule(table, monkeypatch, when, ready):
    from backend.lambda_src import index

    _at(monkeypatch, when)
    body = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert (body["nextReadyDate"], body["readyDateAuto"]) == (ready, ready)


def test_order_is_for_the_automatic_ready_date(table):
    from backend.lambda_src import index

    resp = _order_for_ready_date(index)
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["readyDate"] == "2026-10-26"


def test_order_uses_the_override_for_its_week(table):
    from backend.lambda_src import index

    resp = _order_for_ready_date(index, override="2026-10-27")
    assert json.loads(resp["body"])["readyDate"] == "2026-10-27"


@pytest.mark.parametrize("override, used", [
    ("2026-10-23", True),   # Friday before - 3 days early
    ("2026-10-29", True),   # Thursday after - 3 days late
    ("2026-10-22", False),  # 4 days off
    ("2026-11-02", False),  # next week's Monday - set too early
    ("2026-10-19", False),  # last week's
])
def test_override_only_applies_to_its_own_week(table, override, used):
    from backend.lambda_src import index

    body = json.loads(_put_settings(index, {"readyDateOverride": override})["body"])
    assert body["readyDateOverrideUsed"] is used
    assert body["nextReadyDate"] == (override if used else "2026-10-26")
    # Kept as saved either way, so the admin sees what's there.
    assert body["readyDateOverride"] == override


def test_holiday_override_doesnt_outlive_its_week(table, monkeypatch):
    # Tuesday 2026-10-27 pickup. Once ordering reopens Monday 10-26 at 11 AM,
    # new orders go to 11-02 - not to the Tuesday override the day after.
    from backend.lambda_src import index

    _put_settings(index, {"readyDateOverride": "2026-10-27"})
    _at(monkeypatch, "2026-10-26T16:00:00+00:00")  # Monday 11:00 AM
    body = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert (body["nextReadyDate"], body["readyDateOverrideUsed"]) == ("2026-11-02", False)


def test_clearing_the_override(table):
    from backend.lambda_src import index

    _put_settings(index, {"readyDateOverride": "2026-10-27"})
    body = json.loads(_put_settings(index, {"readyDateOverride": ""})["body"])
    assert (body["nextReadyDate"], body["readyDateOverride"]) == ("2026-10-26", "")


def test_ready_date_saved_before_it_was_automatic_reads_as_an_override(table):
    from backend.lambda_src import index

    index.table.put_item(Item={"PK": "SETTINGS", "SK": "GLOBAL", "nextReadyDate": "2026-10-27", "smsEnabled": False})
    body = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert (body["nextReadyDate"], body["readyDateOverride"]) == ("2026-10-27", "2026-10-27")
    # Saving another setting moves it to the new field.
    _put_settings(index, {"smsEnabled": True})
    stored = index.table.get_item(Key={"PK": "SETTINGS", "SK": "GLOBAL"})["Item"]
    assert stored["readyDateOverride"] == "2026-10-27" and "nextReadyDate" not in stored


@pytest.mark.parametrize("saved", [
    {"nextReadyDate": "2026-10-26"},      # set by hand before it was automatic
    {"readyDateOverride": "2026-10-26"},  # or saved as a "different date"
])
def test_an_override_on_the_automatic_date_is_not_an_override(table, saved):
    # Otherwise Manage menu says "Monday, October 26 (instead of Monday,
    # October 26)".
    from backend.lambda_src import index

    index.table.put_item(Item={"PK": "SETTINGS", "SK": "GLOBAL", **saved})
    body = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert body == {**_settings(), "contactEmail": ""}


def test_forced_open_monday_morning_is_for_next_monday(table, monkeypatch):
    # Nothing can be ordered for a pickup already underway.
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-19T14:00:00+00:00")  # Monday 9:00 AM
    body = json.loads(_put_settings(index, {"orderingMode": "open"})["body"])
    assert body["nextReadyDate"] == "2026-10-26"
    assert body["ordering"] == {"open": True, "reason": "forced"}


def test_list_meals_is_public(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/meals"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"meals": []}


def test_create_meal_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/meals", {"name": "Chicken Bowl", "priceCents": 1200}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 403


def test_create_meal_requires_auth(table):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/meals", {"name": "Chicken Bowl", "priceCents": 1200}), None)
    assert resp["statusCode"] == 401


def test_admin_can_create_update_and_delete_meal(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    meal_id = meal["mealId"]
    assert meal["priceCents"] == 1200

    update_resp = index.handler(
        _event("PUT", f"/meals/{meal_id}", {"priceCents": 1500}, path_params={"mealId": meal_id}, claims=ADMIN_CLAIMS),
        None,
    )
    assert update_resp["statusCode"] == 200
    assert json.loads(update_resp["body"])["priceCents"] == 1500

    delete_resp = index.handler(
        _event("DELETE", f"/meals/{meal_id}", path_params={"mealId": meal_id}, claims=ADMIN_CLAIMS), None
    )
    assert delete_resp["statusCode"] == 204

    list_resp = index.handler(_event("GET", "/meals"), None)
    assert json.loads(list_resp["body"]) == {"meals": []}


def test_create_order_recomputes_total_from_current_menu(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)

    # Client sends a tampered price; server must ignore it.
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 2, "priceCents": 1}],
             "deliveryName": "Ada Lovelace", "deliveryAddress": "123 Main St"},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 2400
    assert order["status"] == "placed"


def test_create_order_adds_global_addon_price(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, description="Large", price_cents=300)
    _offer_addons(index, meal["mealId"], large["addOnId"])

    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"]]}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 1500  # 1200 base + 300 for Large
    assert order["items"][0]["selectedAddOns"] == [
        {
            "addOnId": large["addOnId"], "name": "Large", "priceCents": 300,
            "macros": {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0},
        }
    ]


def test_create_order_snapshots_the_meals_macros_for_the_addon(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    cauli = _create_addon(index, name="Cauliflower Rice", price_cents=100)
    _put_meal(index, meal["mealId"], {"addOnDetails": {
        cauli["addOnId"]: {"description": "Swaps the rice", "macros": {"calories": 410, "proteinG": 40, "carbsG": 12, "fatG": 9}},
    }})

    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [cauli["addOnId"]]}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    order = json.loads(resp["body"])
    assert order["items"][0]["selectedAddOns"][0]["macros"] == {
        "calories": 410, "proteinG": 40, "carbsG": 12, "fatG": 9,
    }

    # Changing the meal's macros afterward must not rewrite the past order.
    _put_meal(index, meal["mealId"], {"addOnDetails": {
        cauli["addOnId"]: {"description": "", "macros": {"calories": 999}},
    }})
    orders_resp = index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)
    stored_order = json.loads(orders_resp["body"])["orders"][0]
    assert stored_order["items"][0]["selectedAddOns"][0]["macros"]["calories"] == 410


def test_addon_without_meal_macros_snapshots_zeros(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    large = _create_addon(index)
    _offer_addons(index, meal["mealId"], large["addOnId"])
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert json.loads(resp["body"])["items"][0]["selectedAddOns"][0]["macros"] == {
        "calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0,
    }


def test_create_order_adds_the_addon_price(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, name="Large", price_cents=300)
    _offer_addons(index, meal["mealId"], large["addOnId"])
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 2, "selectedAddOnIds": [large["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["items"][0]["unitPriceCents"] == 1500
    assert order["totalCents"] == 3000


def test_only_one_addon_per_meal(table):
    # Combinations ("Large with Cauliflower Rice") are add-ons of their own.
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, name="Large", price_cents=300)
    cauli = _create_addon(index, name="Cauliflower Rice", price_cents=100)
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"], cauli["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == "Choose one add-on per meal."


def test_addon_left_blank_on_the_meal_cannot_be_ordered(table):
    # e.g. no cauliflower rice on the tacos - it's offered on other meals.
    from backend.lambda_src import index

    tacos = _create_meal(index, price_cents=1200)
    cauli = _create_addon(index, name="Cauliflower Rice", price_cents=100, description="Swaps the rice")
    _put_meal(index, tacos["mealId"], {"name": "Smash Burger Tacos", "addOnDetails": {cauli["addOnId"]: {"description": ""}}})
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": tacos["mealId"], "quantity": 1, "selectedAddOnIds": [cauli["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == "Cauliflower Rice isn't offered on Smash Burger Tacos."


def test_new_addon_is_not_offered_on_meals_saved_before_it(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    wrap = _create_addon(index, name="Lettuce Wrap", price_cents=0, description="Lettuce instead of tortillas")
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [wrap["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 400


def test_addon_with_only_macros_on_the_meal_is_offered(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, name="Large", price_cents=300)
    _put_meal(index, meal["mealId"], {"addOnDetails": {large["addOnId"]: {"macros": {"calories": 700}}}})
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 201


def test_hidden_addon_cannot_be_ordered(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    large = _create_addon(index)
    _offer_addons(index, meal["mealId"], large["addOnId"])
    _put_addon(index, large["addOnId"], {"available": False})
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 400


def test_create_order_with_no_addons_selected(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    _create_addon(index)  # exists but not selected - shouldn't affect price

    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 1200
    assert order["items"][0]["selectedAddOns"] == []


def test_create_order_stores_item_notes(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1, "note": "  hold the cheese  "}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["items"][0]["note"] == "hold the cheese"


def test_create_order_defaults_note_to_empty_string(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    order = json.loads(resp["body"])
    assert order["items"][0]["note"] == ""


def test_create_order_stores_optional_pickup_name(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}], "pickupName": "  Sam Lee  "},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert json.loads(resp["body"])["pickupName"] == "Sam Lee"
    listed = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])
    assert listed["orders"][0]["pickupName"] == "Sam Lee"


def test_create_order_defaults_pickup_name_to_empty_string(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert json.loads(resp["body"])["pickupName"] == ""


def test_create_order_fails_for_unknown_addon(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": ["does-not-exist"]}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_rejects_unknown_meal(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": "does-not-exist", "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def _order_for_ready_date(index, override=None):
    if override:
        _put_settings(index, {"readyDateOverride": override})
    meal = _create_meal(index)
    return index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]},
               claims=CUSTOMER_CLAIMS),
        None,
    )


# ---------- ordering schedule: Monday 11:00 AM to Friday 4:00 PM Central ----------


@pytest.mark.parametrize("when, is_open", [
    # October 2026 is daylight time (CDT, UTC-5).
    ("2026-10-19T15:59:00+00:00", False),  # Monday 10:59 AM
    ("2026-10-19T16:00:00+00:00", True),   # Monday 11:00 AM - opens
    ("2026-10-21T17:00:00+00:00", True),   # Wednesday noon
    ("2026-10-23T18:59:00+00:00", True),   # Friday 1:59 PM
    ("2026-10-23T19:30:00+00:00", True),   # Friday 2:30 PM - the grace period
    ("2026-10-23T20:59:00+00:00", True),   # Friday 3:59 PM
    ("2026-10-23T21:00:00+00:00", False),  # Friday 4:00 PM - closes
    ("2026-10-24T17:00:00+00:00", False),  # Saturday
    ("2026-10-25T17:00:00+00:00", False),  # Sunday
    # December is standard time (CST, UTC-6).
    ("2026-12-07T16:30:00+00:00", False),  # Monday 10:30 AM
    ("2026-12-07T17:00:00+00:00", True),   # Monday 11:00 AM
    ("2026-12-11T21:59:00+00:00", True),   # Friday 3:59 PM
    ("2026-12-11T22:00:00+00:00", False),  # Friday 4:00 PM
])
def test_ordering_schedule_in_central_time(monkeypatch, when, is_open):
    from backend.lambda_src import index

    _at(monkeypatch, when)
    assert index._ordering_status({}) == {"open": is_open, "reason": "schedule"}


@pytest.mark.parametrize("when, expected_offset", [
    ("2026-03-08T07:59:00+00:00", -6),  # 1:59 AM CST, just before clocks spring forward
    ("2026-03-08T08:00:00+00:00", -5),  # 3:00 AM CDT
    ("2026-11-01T06:59:00+00:00", -5),  # 1:59 AM CDT, just before clocks fall back
    ("2026-11-01T07:00:00+00:00", -6),  # 1:00 AM CST
])
def test_central_time_follows_daylight_saving(when, expected_offset):
    from backend.lambda_src import index

    local = index._central(datetime.fromisoformat(when))
    assert local.utcoffset().total_seconds() == expected_offset * 3600


def test_order_placed_in_the_grace_period_is_accepted(table, monkeypatch):
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-23T20:30:00+00:00")  # Friday 3:30 PM
    assert _order_for_ready_date(index)["statusCode"] == 201


def test_order_placed_after_friday_4pm_is_rejected(table, monkeypatch):
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-23T21:00:00+00:00")  # Friday 4:00 PM
    resp = _order_for_ready_date(index)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == (
        "Ordering is currently closed. Our next menu opens for ordering Monday at 11:00 AM."
    )


def test_settings_report_ordering_closed_on_the_weekend(table, monkeypatch):
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-24T17:00:00+00:00")  # Saturday
    settings = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert settings["ordering"] == {"open": False, "reason": "schedule"}


# ---------- admin override ----------


def _set_mode(index, mode, claims=ADMIN_CLAIMS):
    return index.handler(_event("PUT", "/settings", {"orderingMode": mode}, claims=claims), None)


def test_force_closed_stops_orders_during_the_week(table):
    from backend.lambda_src import index

    resp = _set_mode(index, "closed")
    assert json.loads(resp["body"])["ordering"] == {"open": False, "reason": "forced"}
    assert _order_for_ready_date(index)["statusCode"] == 400


def test_force_open_allows_orders_on_the_weekend(table, monkeypatch):
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-24T17:00:00+00:00")  # Saturday
    _set_mode(index, "open")
    settings = json.loads(index.handler(_event("GET", "/settings"), None)["body"])
    assert (settings["orderingMode"], settings["ordering"]) == ("open", {"open": True, "reason": "forced"})
    assert _order_for_ready_date(index)["statusCode"] == 201


def test_back_to_automatic_follows_the_schedule_again(table, monkeypatch):
    from backend.lambda_src import index

    _at(monkeypatch, "2026-10-24T17:00:00+00:00")  # Saturday
    _set_mode(index, "open")
    resp = _set_mode(index, "auto")
    assert json.loads(resp["body"])["ordering"] == {"open": False, "reason": "schedule"}


def test_saving_the_mode_keeps_the_other_settings(table):
    from backend.lambda_src import index

    _put_settings(index, {"readyDateOverride": "2026-10-27", "smsEnabled": True})
    body = json.loads(_set_mode(index, "closed")["body"])
    assert (body["readyDateOverride"], body["smsEnabled"], body["orderingMode"]) == ("2026-10-27", True, "closed")
    # ...and saving another setting keeps the mode.
    body = json.loads(index.handler(_event("PUT", "/settings", {"smsEnabled": False}, claims=ADMIN_CLAIMS), None)["body"])
    assert body["orderingMode"] == "closed"


def test_invalid_ordering_mode_is_rejected(table):
    from backend.lambda_src import index

    assert _set_mode(index, "sometimes")["statusCode"] == 400


def test_only_admin_can_override_ordering(table):
    from backend.lambda_src import index

    assert _set_mode(index, "open", claims=CUSTOMER_CLAIMS)["statusCode"] == 403


def test_admin_cannot_place_an_order(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_list_orders_is_scoped_to_the_caller(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    other_claims = {"sub": "user-2", "email": "other@example.com", "name": "Olive Other"}
    index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 3}]}, claims=other_claims),
        None,
    )

    resp = index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)
    orders = json.loads(resp["body"])["orders"]
    assert len(orders) == 1
    assert orders[0]["items"][0]["quantity"] == 1


def _place_order(index, claims, quantity=1):
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": quantity}]}, claims=claims),
        None,
    )
    return json.loads(resp["body"])


def test_customer_can_cancel_a_placed_order(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    resp = index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "cancelled"


def test_customer_cannot_cancel_an_already_cancelled_order(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    resp = index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_customer_cannot_cancel_someone_elses_order(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    other_claims = {"sub": "user-2", "email": "other@example.com", "name": "Olive Other"}
    resp = index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=other_claims),
        None,
    )
    assert resp["statusCode"] == 400


def test_admin_orders_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/admin/orders", claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 403


def test_admin_can_list_placed_orders_across_customers(table):
    from backend.lambda_src import index

    order1 = _place_order(index, CUSTOMER_CLAIMS)
    other_claims = {"sub": "user-2", "email": "other@example.com", "name": "Olive Other"}
    order2 = _place_order(index, other_claims, quantity=2)

    resp = index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    orders = json.loads(resp["body"])["orders"]
    assert {o["orderId"] for o in orders} == {order1["orderId"], order2["orderId"]}
    assert {o["customerSub"] for o in orders} == {"user-1", "user-2"}


def test_admin_orders_list_excludes_non_placed_orders(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    resp = index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS), None)
    assert json.loads(resp["body"])["orders"] == []


def test_admin_orders_status_all_returns_every_status(table):
    from backend.lambda_src import index

    placed = _place_order(index, CUSTOMER_CLAIMS)
    cancelled = _place_order(index, CUSTOMER_CLAIMS)
    index.handler(
        _event("PUT", f"/orders/{cancelled['orderId']}", {"status": "cancelled"},
               path_params={"orderId": cancelled["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )

    resp = index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS, query={"status": "all"}), None)
    assert resp["statusCode"] == 200
    orders = json.loads(resp["body"])["orders"]
    assert {o["orderId"] for o in orders} == {placed["orderId"], cancelled["orderId"]}


def test_admin_orders_rejects_invalid_status_filter(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS, query={"status": "bogus"}), None)
    assert resp["statusCode"] == 400


def test_update_order_status_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/admin/orders/user-1/abc", {"status": "prepared"},
               path_params={"sub": "user-1", "orderId": "abc"}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 403


def test_admin_can_update_order_status(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    resp = index.handler(
        _event("PUT", f"/admin/orders/user-1/{order['orderId']}", {"status": "prepared"},
               path_params={"sub": "user-1", "orderId": order["orderId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "prepared"
    assert body["customerSub"] == "user-1"

    # No longer in the "placed" queue once moved along.
    resp = index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS), None)
    assert json.loads(resp["body"])["orders"] == []


def test_admin_update_order_status_rejects_invalid_status(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    resp = index.handler(
        _event("PUT", f"/admin/orders/user-1/{order['orderId']}", {"status": "bogus"},
               path_params={"sub": "user-1", "orderId": order["orderId"]}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_unknown_route_returns_404(table):
    from backend.lambda_src import index

    resp = index.handler(_event("DELETE", "/nope"), None)
    assert resp["statusCode"] == 404


def test_profile_requires_auth(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/profile"), None)
    assert resp["statusCode"] == 401


def test_get_profile_defaults_to_empty_with_email_from_claims(table):
    from backend.lambda_src import index

    no_name_claims = {"sub": "user-1", "email": "customer@example.com"}
    resp = index.handler(_event("GET", "/profile", claims=no_name_claims), None)
    assert resp["statusCode"] == 200
    profile = json.loads(resp["body"])
    assert profile == {
        "name": "", "email": "customer@example.com", "phone": "", "address": "",
        "preferredContact": "email", "unsubscribed": False, "pickupLocation": "",
    }


def test_update_and_get_profile_roundtrip(table):
    from backend.lambda_src import index

    update_resp = index.handler(
        _event(
            "PUT", "/profile",
            {
                "name": "Ada Lovelace", "email": "ada@example.com", "phone": "555-1234", "address": "123 Main St",
                "preferredContact": "text", "unsubscribed": True,
            },
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert update_resp["statusCode"] == 200
    assert json.loads(update_resp["body"])["name"] == "Ada Lovelace"

    get_resp = index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)
    profile = json.loads(get_resp["body"])
    assert profile == {
        "name": "Ada Lovelace", "email": "ada@example.com", "phone": "555-1234", "address": "123 Main St",
        "preferredContact": "text", "unsubscribed": True, "pickupLocation": "",
    }


def test_partial_profile_update_keeps_notification_settings(table):
    # Checkout saves only name/email/phone/address back after an order -
    # that must not reset a texting customer to email or re-subscribe
    # someone who opted out.
    from backend.lambda_src import index

    index.handler(
        _event("PUT", "/profile", {"name": "Ada", "preferredContact": "text", "unsubscribed": True},
               claims=CUSTOMER_CLAIMS),
        None,
    )
    index.handler(
        _event("PUT", "/profile",
               {"name": "Ada L", "email": "ada@example.com", "phone": "555-1234", "address": "1 Main St"},
               claims=CUSTOMER_CLAIMS),
        None,
    )
    profile = json.loads(index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)["body"])
    assert profile == {
        "name": "Ada L", "email": "ada@example.com", "phone": "555-1234", "address": "1 Main St",
        "preferredContact": "text", "unsubscribed": True, "pickupLocation": "",
    }


def test_partial_profile_update_keeps_other_text_fields(table):
    from backend.lambda_src import index

    index.handler(
        _event("PUT", "/profile", {"name": "Ada", "phone": "555-1234", "address": "1 Main St"},
               claims=CUSTOMER_CLAIMS),
        None,
    )
    index.handler(_event("PUT", "/profile", {"unsubscribed": True}, claims=CUSTOMER_CLAIMS), None)
    profile = json.loads(index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)["body"])
    assert (profile["name"], profile["phone"], profile["address"]) == ("Ada", "555-1234", "1 Main St")
    assert profile["unsubscribed"] is True


def test_profile_can_still_clear_a_field_explicitly(table):
    from backend.lambda_src import index

    index.handler(_event("PUT", "/profile", {"phone": "555-1234"}, claims=CUSTOMER_CLAIMS), None)
    index.handler(_event("PUT", "/profile", {"phone": ""}, claims=CUSTOMER_CLAIMS), None)
    profile = json.loads(index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)["body"])
    assert profile["phone"] == ""


def test_get_profile_falls_back_to_sign_up_name(table):
    from backend.lambda_src import index

    claims = {**CUSTOMER_CLAIMS, "name": "Pat Lee"}
    profile = json.loads(index.handler(_event("GET", "/profile", claims=claims), None)["body"])
    assert profile["name"] == "Pat Lee"


def test_update_profile_rejects_invalid_preferred_contact(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/profile", {"preferredContact": "carrier-pigeon"}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 400


def test_profile_is_scoped_to_the_caller(table):
    from backend.lambda_src import index

    index.handler(
        _event("PUT", "/profile", {"name": "Ada Lovelace"}, claims=CUSTOMER_CLAIMS), None
    )
    other_claims = {"sub": "user-2", "email": "other@example.com"}
    resp = index.handler(_event("GET", "/profile", claims=other_claims), None)
    assert json.loads(resp["body"])["name"] == ""


def _order(index, claims=CUSTOMER_CLAIMS, **body):
    meal = _create_meal(index)
    return index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}], **body}, claims=claims),
        None,
    )


def test_order_takes_customer_name_from_profile_not_the_request(table):
    from backend.lambda_src import index

    _save_profile(index, CUSTOMER_CLAIMS, name="Ada Lovelace")
    resp = _order(index, contactPhone=PHONE, customerName="Someone Else", deliveryName="Someone Else")
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["customerName"] == "Ada Lovelace"


def test_order_falls_back_to_sign_up_name_without_a_profile(table):
    from backend.lambda_src import index

    order = json.loads(_order(index, contactPhone=PHONE)["body"])
    assert order["customerName"] == "Casey Customer"


def test_order_rejected_when_customer_has_no_name(table):
    from backend.lambda_src import index

    resp = _order(index, claims={"sub": "user-9", "email": "x@example.com"}, contactPhone=PHONE)
    assert resp["statusCode"] == 400
    assert "My Account" in json.loads(resp["body"])["message"]


def test_order_stores_its_own_contact_phone(table):
    # Prefilled from the profile at checkout, but stored per order - and
    # placing the order doesn't change the profile's phone.
    from backend.lambda_src import index

    _save_profile(index, CUSTOMER_CLAIMS, name="Ada", phone="(555) 111-1111")
    order = json.loads(_order(index, contactPhone="(555) 999-9999")["body"])
    assert order["contactPhone"] == "(555) 999-9999"
    profile = json.loads(index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)["body"])
    assert profile["phone"] == "(555) 111-1111"


@pytest.mark.parametrize("phone", ["555-1234", "(555) 555-12345", "call me"])
def test_order_rejects_a_partial_contact_phone(table, phone):
    from backend.lambda_src import index

    resp = _order(index, contactPhone=phone)
    assert resp["statusCode"] == 400
    assert "phone" in json.loads(resp["body"])["message"]


@pytest.mark.parametrize("body", [{}, {"contactPhone": ""}, {"contactPhone": "   "}])
def test_contact_phone_is_optional(table, body):
    from backend.lambda_src import index

    resp = _order(index, **body)
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["contactPhone"] == ""


def test_order_no_longer_stores_email_or_address(table):
    from backend.lambda_src import index

    order = json.loads(
        _order(index, contactPhone=PHONE, deliveryEmail="a@example.com", deliveryAddress="1 Main St")["body"]
    )
    assert "deliveryEmail" not in order and "deliveryAddress" not in order
    stored = index.table.get_item(Key={"PK": "USER#user-1", "SK": f"ORDER#{order['createdAt']}#{order['orderId']}"})
    assert "deliveryEmail" not in stored["Item"] and "deliveryAddress" not in stored["Item"]


def test_orders_placed_before_the_change_still_show_name_and_phone(table):
    from backend.lambda_src import index

    index.table.put_item(Item={
        "PK": "USER#user-1", "SK": "ORDER#2026-01-01T00:00:00+00:00#old1", "orderId": "old1",
        "createdAt": "2026-01-01T00:00:00+00:00", "items": [], "totalCents": 0, "status": "delivered",
        "deliveryName": "Old Name", "deliveryEmail": "old@example.com",
        "deliveryPhone": "(555) 000-0000", "deliveryAddress": "Old St",
    })
    [order] = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])["orders"]
    assert (order["customerName"], order["contactPhone"], order["pickupName"]) == ("Old Name", "(555) 000-0000", "")


def _save_profile(index, claims, **fields):
    body = {"preferredContact": "email", "unsubscribed": False}
    body.update(fields)
    index.handler(_event("PUT", "/profile", body, claims=claims), None)


def test_reminders_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/reminders/send", claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 403


def _enable_sms(index):
    index.handler(_event("PUT", "/settings", {"smsEnabled": True}, claims=ADMIN_CLAIMS), None)


def test_reminders_sends_by_preference_and_skips_unsubscribed(table, monkeypatch):
    import boto3
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    # moto's SES mock enforces the same verified-sender rule real SES does.
    _verify_sender_domain()
    _enable_sms(index)
    # Texting goes through Twilio, not a boto3 client moto can intercept -
    # stub it out rather than needing real Secrets Manager + network calls.
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: None)

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")
    _save_profile(index, {"sub": "u2", "email": "u2@example.com"}, phone="+15551234567", preferredContact="text")
    _save_profile(index, {"sub": "u3", "email": "u3@example.com"}, email="u3@example.com", unsubscribed=True)

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    result = json.loads(resp["body"])
    assert result["sentEmail"] == 1
    assert result["sentText"] == 1
    assert result["skippedUnsubscribed"] == 1
    assert result["skippedSmsDisabled"] == 0
    assert result["failed"] == 0


def test_reminders_counts_failure_when_contact_detail_missing(table, capsys):
    import boto3
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    _verify_sender_domain()
    _enable_sms(index)

    # preferredContact=text but no phone on file
    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, preferredContact="text")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    result = json.loads(resp["body"])
    assert result["failed"] == 1
    assert result["sentText"] == 0
    # Logged as an ERROR, for the API log-errors alarm.
    assert "ERROR sending to USER#u1" in capsys.readouterr().out


def test_reminders_skip_sms_when_globally_disabled(table, monkeypatch):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    # smsEnabled defaults to False - never explicitly turned on in this test.

    publish_calls = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: publish_calls.append((phone, message)))

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, phone="+15551234567", preferredContact="text")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    result = json.loads(resp["body"])
    assert result["sentText"] == 0
    assert result["skippedSmsDisabled"] == 1
    assert result["failed"] == 0
    assert publish_calls == []


def test_reminders_sends_sms_once_globally_enabled(table, monkeypatch):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    _enable_sms(index)

    publish_calls = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: publish_calls.append((phone, message)))

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, phone="+15551234567", preferredContact="text")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    result = json.loads(resp["body"])
    assert result["sentText"] == 1
    assert result["skippedSmsDisabled"] == 0
    assert len(publish_calls) == 1
    assert publish_calls[0][0] == "+15551234567"


def test_reminders_send_without_reply_to_when_no_admin_is_configured(table, monkeypatch):
    from backend.lambda_src import index

    # A real pool with an Admins group that has no members - the sender is a
    # fixed address, so email still goes out, just without a Reply-To.
    cognito, pool_id = _create_test_pool(index)
    u1 = _create_cognito_user(cognito, pool_id, "u1@example.com")
    _save_profile(index, {"sub": u1, "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")

    sent = []
    monkeypatch.setattr(index.ses, "send_email", lambda **kwargs: sent.append(kwargs))

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    assert len(sent) == 1
    assert sent[0]["ReplyToAddresses"] == []


def test_reminders_fail_cleanly_when_no_sender_is_configured(table, monkeypatch):
    from backend.lambda_src import index

    # FROM_EMAIL is empty when the stack has no custom domain - fail the whole
    # request up front rather than fail every single recipient one at a time.
    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    monkeypatch.setattr(index, "FROM_EMAIL", "")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400


def test_menu_send_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/menu/send", {"subject": "Menu", "message": "Here is our weekly menu"}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 403


def test_menu_send_requires_subject_and_message(table):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/menu/send", {"message": "hi"}, claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400

    resp = index.handler(_event("POST", "/menu/send", {"subject": "Menu"}, claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400


def test_menu_send_routes_by_preferred_contact(table, monkeypatch):
    import boto3
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    _verify_sender_domain()
    _enable_sms(index)

    text_calls = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: text_calls.append((phone, message)))

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")
    _save_profile(index, {"sub": "u2", "email": "u2@example.com"}, phone="+15551234567", preferredContact="text")
    _save_profile(index, {"sub": "u3", "email": "u3@example.com"}, email="u3@example.com", unsubscribed=True)

    resp = index.handler(
        _event("POST", "/menu/send",
               {"subject": "GTX Meals - This week's menu", "message": "Here is our weekly menu: https://gtxmeals.com/menu-flyer.html"},
               claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 200
    result = json.loads(resp["body"])
    assert result["sentEmail"] == 1
    assert result["sentText"] == 1
    assert result["skippedUnsubscribed"] == 1
    assert result["skippedSmsDisabled"] == 0
    assert result["skippedNoContact"] == 0
    assert result["failed"] == 0
    assert len(text_calls) == 1
    assert text_calls[0] == ("+15551234567", "Here is our weekly menu: https://gtxmeals.com/menu-flyer.html")


def test_menu_send_skips_text_when_sms_disabled_and_counts_missing_contact(table, monkeypatch):
    from backend.lambda_src import index

    # send_menu_notification resolves the sender and Reply-To up front
    # regardless of channel mix, same as send_weekly_reminders.
    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")

    # smsEnabled defaults to False - never turned on in this test.
    text_calls = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: text_calls.append((phone, message)))

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, preferredContact="text")  # no phone on file
    _save_profile(index, {"sub": "u2", "email": "u2@example.com"}, phone="+15551234567", preferredContact="text")

    resp = index.handler(
        _event("POST", "/menu/send", {"subject": "Menu", "message": "Here is our weekly menu"}, claims=ADMIN_CLAIMS),
        None,
    )
    result = json.loads(resp["body"])
    # Both are gated by smsEnabled being off before the missing-phone check
    # is ever reached, so neither one calls the webhook.
    assert result["sentText"] == 0
    assert result["skippedSmsDisabled"] == 2
    assert text_calls == []


def test_send_text_via_twilio_builds_correct_request(table, monkeypatch):
    import base64

    from backend.lambda_src import index

    secret = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name="test-twilio-secret",
        SecretString=json.dumps({"accountSid": "ACxxxx", "authToken": "secret-token", "fromNumber": "+15550001111"}),
    )
    monkeypatch.setattr(index, "TWILIO_SECRET_ARN", secret["ARN"])

    captured = {}

    def fake_urlopen(request, timeout=10):
        captured["url"] = request.full_url
        captured["auth_header"] = request.get_header("Authorization")
        captured["body"] = request.data.decode("utf-8")

    monkeypatch.setattr(index.urllib.request, "urlopen", fake_urlopen)

    index._send_text_via_twilio("+15559998888", "Here is our weekly menu")

    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACxxxx/Messages.json"
    decoded = base64.b64decode(captured["auth_header"].split(" ")[1]).decode()
    assert decoded == "ACxxxx:secret-token"
    assert "To=%2B15559998888" in captured["body"]
    assert "From=%2B15550001111" in captured["body"]


def test_send_text_via_twilio_raises_when_not_configured(table):
    from backend.lambda_src import index

    assert index.TWILIO_SECRET_ARN == ""
    with pytest.raises(RuntimeError):
        index._send_text_via_twilio("+15559998888", "hi")


def _configure_unsubscribe_secret(index, monkeypatch):
    secret = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name="test-unsubscribe-secret", SecretString="test-signing-key",
    )
    monkeypatch.setattr(index, "UNSUBSCRIBE_SECRET_ARN", secret["ARN"])


def test_unsubscribe_rejects_missing_or_wrong_token(table, monkeypatch):
    from backend.lambda_src import index

    _configure_unsubscribe_secret(index, monkeypatch)

    # No token at all.
    resp = index.handler(_event("GET", "/unsubscribe", query={"email": "u1@example.com"}), None)
    assert resp["statusCode"] == 403

    # Wrong token - e.g. a guess, or one signed for a different email.
    resp = index.handler(
        _event("GET", "/unsubscribe", query={"email": "u1@example.com", "token": "not-the-real-token"}), None,
    )
    assert resp["statusCode"] == 403


def test_unsubscribe_sets_flag_for_matching_profile(table, monkeypatch):
    from backend.lambda_src import index

    _configure_unsubscribe_secret(index, monkeypatch)
    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")

    token = index._unsubscribe_token("u1@example.com")
    resp = index.handler(
        _event("GET", "/unsubscribe", query={"email": "u1@example.com", "token": token}), None,
    )
    assert resp["statusCode"] == 200
    assert "unsubscribed" in resp["body"]

    profile_resp = index.handler(_event("GET", "/profile", claims={"sub": "u1", "email": "u1@example.com"}), None)
    assert json.loads(profile_resp["body"])["unsubscribed"] is True


def test_unsubscribe_is_safe_when_no_matching_profile(table, monkeypatch):
    from backend.lambda_src import index

    _configure_unsubscribe_secret(index, monkeypatch)

    token = index._unsubscribe_token("nobody@example.com")
    resp = index.handler(
        _event("GET", "/unsubscribe", query={"email": "nobody@example.com", "token": token}), None,
    )
    # Doesn't leak whether the address is a real customer - just confirms no more mail.
    assert resp["statusCode"] == 200


def test_menu_send_appends_unsubscribe_link_to_email_body(table, monkeypatch):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    _verify_sender_domain()
    _configure_unsubscribe_secret(index, monkeypatch)

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")

    sent = []
    monkeypatch.setattr(index.ses, "send_email", lambda **kwargs: sent.append(kwargs))

    resp = index.handler(
        _event("POST", "/menu/send", {"subject": "Menu", "message": "Here is our menu"}, claims=ADMIN_CLAIMS), None,
    )
    assert resp["statusCode"] == 200
    assert len(sent) == 1
    body_text = sent[0]["Message"]["Body"]["Text"]["Data"]
    assert "Here is our menu" in body_text
    assert "/unsubscribe?" in body_text
    assert "email=u1%40example.com" in body_text
    assert f"token={index._unsubscribe_token('u1@example.com')}" in body_text
    # Sent from the site's own domain, with replies going to the admin.
    assert sent[0]["Source"] == "GTX Meals <menu@example.com>"
    assert sent[0]["ReplyToAddresses"] == ["admin@example.com"]


def _verify_sender_domain():
    boto3.client("ses", region_name="us-east-1").verify_domain_identity(Domain="example.com")


def _create_test_pool(index):
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    # UsernameAttributes=["email"] matches the real deployed pool: Cognito then
    # auto-generates a UUID as the real Username, and email is only a login
    # alias it accepts interchangeably in admin_* calls. Without this, moto
    # would just use the email as the literal Username and hide bugs that only
    # show up against the real pool's aliasing (e.g. comparing a path-param
    # username directly against a canonical-Username set).
    pool_id = cognito.create_user_pool(PoolName="test-pool", UsernameAttributes=["email"])["UserPool"]["Id"]
    cognito.create_group(UserPoolId=pool_id, GroupName="Admins")
    index.USER_POOL_ID = pool_id
    return cognito, pool_id


def _create_cognito_user(cognito, pool_id, email):
    created = cognito.admin_create_user(
        UserPoolId=pool_id, Username=email,
        UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
        MessageAction="SUPPRESS",
    )
    return next(a["Value"] for a in created["User"]["Attributes"] if a["Name"] == "sub")


def _create_admin_cognito_user(cognito, pool_id, email):
    sub = _create_cognito_user(cognito, pool_id, email)
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName="Admins")
    return sub


def test_customers_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/customers", claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 403


def test_list_customers_includes_accounts_without_a_saved_profile(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "noprofile@example.com")

    resp = index.handler(_event("GET", "/customers", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    customers = json.loads(resp["body"])["customers"]
    assert len(customers) == 1
    # The real Cognito Username is an auto-generated UUID (UsernameAttributes
    # = ["email"]), which happens to equal "sub" for this pool.
    assert customers[0]["username"] == sub
    assert customers[0]["email"] == "noprofile@example.com"  # falls back to Cognito email
    assert customers[0]["name"] == ""


def test_list_customers_merges_in_saved_profile(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    _save_profile(index, {"sub": sub, "email": "jane@example.com"}, name="Jane Doe", phone="555-1111")

    resp = index.handler(_event("GET", "/customers", claims=ADMIN_CLAIMS), None)
    customers = json.loads(resp["body"])["customers"]
    assert len(customers) == 1
    assert customers[0]["name"] == "Jane Doe"
    assert customers[0]["phone"] == "555-1111"


def test_list_customers_excludes_admin_accounts(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    customer_sub = _create_cognito_user(cognito, pool_id, "customer@example.com")
    cognito.admin_create_user(
        UserPoolId=pool_id, Username="admin@example.com",
        UserAttributes=[{"Name": "email", "Value": "admin@example.com"}, {"Name": "email_verified", "Value": "true"}],
        MessageAction="SUPPRESS",
    )
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username="admin@example.com", GroupName="Admins")

    resp = index.handler(_event("GET", "/customers", claims=ADMIN_CLAIMS), None)
    customers = json.loads(resp["body"])["customers"]
    assert [c["username"] for c in customers] == [customer_sub]


def test_update_customer_rejects_an_admin_account(table):
    # Regression test: the path param here is the email alias, not the real
    # Username (a UUID, since the pool's UsernameAttributes=["email"]) - an
    # earlier version of this check compared the alias directly against a set
    # of real Usernames and never matched, silently letting this through.
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    cognito.admin_create_user(
        UserPoolId=pool_id, Username="admin@example.com",
        UserAttributes=[{"Name": "email", "Value": "admin@example.com"}, {"Name": "email_verified", "Value": "true"}],
        MessageAction="SUPPRESS",
    )
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username="admin@example.com", GroupName="Admins")

    resp = index.handler(
        _event("PUT", "/customers/admin@example.com", {"name": "x"},
               path_params={"username": "admin@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_delete_customer_rejects_an_admin_account(table):
    # See the comment in test_update_customer_rejects_an_admin_account - same
    # email-alias-vs-UUID regression, but for delete, which is unrecoverable.
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    cognito.admin_create_user(
        UserPoolId=pool_id, Username="admin@example.com",
        UserAttributes=[{"Name": "email", "Value": "admin@example.com"}, {"Name": "email_verified", "Value": "true"}],
        MessageAction="SUPPRESS",
    )
    cognito.admin_add_user_to_group(UserPoolId=pool_id, Username="admin@example.com", GroupName="Admins")

    resp = index.handler(
        _event("DELETE", "/customers/admin@example.com",
               path_params={"username": "admin@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400
    # Still there - rejecting the request must not have deleted it anyway.
    cognito.admin_get_user(UserPoolId=pool_id, Username="admin@example.com")


def test_admin_can_update_customer(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_cognito_user(cognito, pool_id, "jane@example.com")

    resp = index.handler(
        _event(
            "PUT", "/customers/jane@example.com",
            {"name": "Jane Updated", "phone": "555-2222", "preferredContact": "text", "unsubscribed": True},
            path_params={"username": "jane@example.com"}, claims=ADMIN_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["name"] == "Jane Updated"
    assert body["unsubscribed"] is True

    list_resp = index.handler(_event("GET", "/customers", claims=ADMIN_CLAIMS), None)
    customers = json.loads(list_resp["body"])["customers"]
    assert customers[0]["phone"] == "555-2222"


def test_update_customer_fails_for_unknown_username(table):
    from backend.lambda_src import index

    _create_test_pool(index)
    resp = index.handler(
        _event("PUT", "/customers/ghost@example.com", {"name": "x"}, path_params={"username": "ghost@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_admin_can_delete_customer(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    _save_profile(index, {"sub": sub, "email": "jane@example.com"}, name="Jane Doe")

    resp = index.handler(
        _event("DELETE", "/customers/jane@example.com", path_params={"username": "jane@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 204

    with pytest.raises(cognito.exceptions.UserNotFoundException):
        cognito.admin_get_user(UserPoolId=pool_id, Username="jane@example.com")

    list_resp = index.handler(_event("GET", "/customers", claims=ADMIN_CLAIMS), None)
    assert json.loads(list_resp["body"])["customers"] == []


def test_delete_customer_removes_both_cognito_account_and_profile(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    _save_profile(index, {"sub": sub, "email": "jane@example.com"}, name="Jane Doe", phone="555-1234")
    assert "Item" in index.table.get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"})

    resp = index.handler(
        _event("DELETE", "/customers/jane@example.com", path_params={"username": "jane@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 204

    with pytest.raises(cognito.exceptions.UserNotFoundException):
        cognito.admin_get_user(UserPoolId=pool_id, Username="jane@example.com")
    assert "Item" not in index.table.get_item(Key={"PK": f"USER#{sub}", "SK": "PROFILE"})


def test_delete_customer_can_be_retried_if_cognito_fails(table, monkeypatch):
    # The profile row goes first, then the Cognito account. If Cognito
    # fails, the login still exists, so the admin's retry can still find
    # the customer and finish the job instead of leaving a half-deleted
    # account behind.
    from botocore.exceptions import ClientError

    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    _save_profile(index, {"sub": sub, "email": "jane@example.com"}, name="Jane Doe")
    event = _event("DELETE", "/customers/jane@example.com", path_params={"username": "jane@example.com"},
                   claims=ADMIN_CLAIMS)

    real_delete = index.cognito_idp.admin_delete_user

    def failing_delete(**kwargs):
        raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}}, "AdminDeleteUser")

    monkeypatch.setattr(index.cognito_idp, "admin_delete_user", failing_delete)
    with pytest.raises(ClientError):
        index.handler(event, None)
    # Not reported as deleted, and the login is still there to retry against.
    cognito.admin_get_user(UserPoolId=pool_id, Username="jane@example.com")

    monkeypatch.setattr(index.cognito_idp, "admin_delete_user", real_delete)
    assert index.handler(event, None)["statusCode"] == 204
    with pytest.raises(cognito.exceptions.UserNotFoundException):
        cognito.admin_get_user(UserPoolId=pool_id, Username="jane@example.com")


def test_delete_customer_does_not_delete_their_orders(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    meal = _create_meal(index)
    index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims={"sub": sub, "email": "jane@example.com", "name": "Jane Doe"}),
        None,
    )

    index.handler(
        _event("DELETE", "/customers/jane@example.com", path_params={"username": "jane@example.com"}, claims=ADMIN_CLAIMS),
        None,
    )

    orders = table_resource_scan_orders(sub)
    assert len(orders) == 1


def table_resource_scan_orders(sub):
    import boto3 as _boto3
    dynamodb = _boto3.resource("dynamodb", region_name="us-east-1")
    t = dynamodb.Table("MealPrepTable")
    resp = t.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": f"USER#{sub}", ":sk": "ORDER#"},
    )
    return resp.get("Items", [])


# ---------- add-on name + description ----------


def _put_addon(index, addon_id, body):
    return index.handler(
        _event("PUT", f"/addons/{addon_id}", body, path_params={"addOnId": addon_id}, claims=ADMIN_CLAIMS), None
    )


def test_addon_has_separate_name_and_description(table):
    from backend.lambda_src import index

    addon = _create_addon(index, name="  Large ", description=" Includes 2 extra ounces of protein. ")
    assert (addon["name"], addon["description"]) == ("Large", "Includes 2 extra ounces of protein.")
    [listed] = json.loads(index.handler(_event("GET", "/addons"), None)["body"])["addOns"]
    assert (listed["name"], listed["description"]) == ("Large", "Includes 2 extra ounces of protein.")


def test_addon_description_is_optional(table):
    from backend.lambda_src import index

    addon = _create_addon(index, name="Extra Protein")
    assert addon["description"] == ""


@pytest.mark.parametrize("body", [{"priceCents": 100}, {"name": "  ", "priceCents": 100}])
def test_addon_name_is_required(table, body):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/addons", body, claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == "Add-on name is required"


def test_updating_only_the_description_keeps_the_name(table):
    from backend.lambda_src import index

    addon = _create_addon(index, name="Large")
    updated = json.loads(_put_addon(index, addon["addOnId"], {"description": "Bigger portion"})["body"])
    assert (updated["name"], updated["description"]) == ("Large", "Bigger portion")


def test_updating_with_a_blank_name_is_rejected(table):
    from backend.lambda_src import index

    addon = _create_addon(index, name="Large")
    assert _put_addon(index, addon["addOnId"], {"name": ""})["statusCode"] == 400


def _put_legacy_addon(index):
    # Saved before the split: the short name lived in "description".
    index.table.put_item(Item={
        "PK": "ADDON", "SK": "old-addon", "addOnId": "old-addon",
        "description": "Large", "priceCents": 300, "available": True,
    })


def test_legacy_addon_reads_its_description_as_the_name(table):
    from backend.lambda_src import index

    _put_legacy_addon(index)
    [addon] = json.loads(index.handler(_event("GET", "/addons"), None)["body"])["addOns"]
    assert (addon["name"], addon["description"]) == ("Large", "")


def test_editing_a_legacy_addon_adds_a_description_without_losing_the_name(table):
    from backend.lambda_src import index

    _put_legacy_addon(index)
    updated = json.loads(_put_addon(index, "old-addon", {"description": "Includes 2 extra ounces of protein."})["body"])
    assert (updated["name"], updated["description"]) == ("Large", "Includes 2 extra ounces of protein.")
    stored = index.table.get_item(Key={"PK": "ADDON", "SK": "old-addon"})["Item"]
    assert (stored["name"], stored["description"]) == ("Large", "Includes 2 extra ounces of protein.")


def test_ordering_a_legacy_addon_snapshots_its_name(table):
    from backend.lambda_src import index

    _put_legacy_addon(index)
    meal = _create_meal(index)
    _offer_addons(index, meal["mealId"], "old-addon")
    resp = index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE,
        "items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": ["old-addon"]}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert json.loads(resp["body"])["items"][0]["selectedAddOns"][0]["name"] == "Large"


def test_orders_placed_before_the_split_show_the_addon_name(table):
    from backend.lambda_src import index

    index.table.put_item(Item={
        "PK": "USER#user-1", "SK": "ORDER#2026-01-01T00:00:00+00:00#old2", "orderId": "old2",
        "createdAt": "2026-01-01T00:00:00+00:00", "totalCents": 1500, "status": "delivered",
        "items": [{"mealId": "m1", "name": "Bowl", "unitPriceCents": 1500, "quantity": 1, "note": "",
                   "selectedAddOns": [{"addOnId": "a1", "description": "Large", "priceCents": 300}]}],
    })
    [order] = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])["orders"]
    assert order["items"][0]["selectedAddOns"] == [{"addOnId": "a1", "name": "Large", "priceCents": 300}]


# ---------- edit/cancel deadline: Friday 4:00 PM (customers are told 2:00 PM) ----------


def _order_for_monday(index):
    # Placed Wednesday 2026-10-21 (the pinned clock) for Monday 2026-10-26.
    order = json.loads(_order_for_ready_date(index)["body"])
    assert order["readyDate"] == "2026-10-26"
    return order


def _cancel(index, order):
    return index.handler(
        _event("PUT", f"/orders/{order['orderId']}", {"status": "cancelled"},
               path_params={"orderId": order["orderId"]}, claims=CUSTOMER_CLAIMS),
        None,
    )


def _listed(index):
    [order] = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])["orders"]
    return order


@pytest.mark.parametrize("when, changeable", [
    ("2026-10-21T17:00:00+00:00", True),   # Wednesday
    ("2026-10-23T19:00:00+00:00", True),   # Friday 2:00 PM - grace period
    ("2026-10-23T20:59:00+00:00", True),   # Friday 3:59 PM
    ("2026-10-23T21:00:00+00:00", False),  # Friday 4:00 PM
    ("2026-10-24T17:00:00+00:00", False),  # Saturday
])
def test_order_is_changeable_until_friday_4pm(table, monkeypatch, when, changeable):
    from backend.lambda_src import index

    _order_for_monday(index)
    _at(monkeypatch, when)
    assert _listed(index)["changeable"] is changeable


def test_cancel_before_the_deadline_works(table, monkeypatch):
    from backend.lambda_src import index

    order = _order_for_monday(index)
    _at(monkeypatch, "2026-10-23T20:30:00+00:00")  # Friday 3:30 PM
    resp = _cancel(index, order)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "cancelled"


def test_cancel_after_the_deadline_is_refused(table, monkeypatch):
    from backend.lambda_src import index

    order = _order_for_monday(index)
    _at(monkeypatch, "2026-10-23T21:00:00+00:00")  # Friday 4:00 PM
    resp = _cancel(index, order)
    assert resp["statusCode"] == 400
    assert "Friday at 2:00 PM" in json.loads(resp["body"])["message"]
    assert _listed(index)["status"] == "placed"


def test_orders_cant_be_changed_while_ordering_is_forced_closed(table):
    # Edit cancels then re-orders - with ordering closed, the customer
    # couldn't place it again, so don't let them start.
    from backend.lambda_src import index

    order = _order_for_monday(index)
    _set_mode(index, "closed")
    assert _listed(index)["changeable"] is False
    assert _cancel(index, order)["statusCode"] == 400


def test_last_weeks_order_is_not_changeable_even_if_forced_open(table, monkeypatch):
    from backend.lambda_src import index

    _order_for_monday(index)
    _set_mode(index, "open")
    _at(monkeypatch, "2026-10-24T17:00:00+00:00")  # Saturday - past that order's Friday
    assert _listed(index)["changeable"] is False


def test_cancelled_orders_are_not_changeable(table):
    from backend.lambda_src import index

    order = _order_for_monday(index)
    _cancel(index, order)
    assert _listed(index)["changeable"] is False


@pytest.mark.parametrize("ready_date, friday", [
    ("2026-10-26", "2026-10-23"),  # Monday -> the Friday before
    ("2026-10-27", "2026-10-23"),  # a Tuesday (holiday week) -> still that Friday
    ("2026-10-30", "2026-10-23"),  # a Friday -> the Friday a week earlier
])
def test_change_deadline_is_the_friday_before_the_ready_date(ready_date, friday):
    from backend.lambda_src import index

    tz = index._central(OPEN_TIME).tzinfo
    just_before = datetime.fromisoformat(f"{friday}T15:59:00").replace(tzinfo=tz)
    at_four = datetime.fromisoformat(f"{friday}T16:00:00").replace(tzinfo=tz)
    assert index._change_deadline_passed(ready_date, just_before) is False
    assert index._change_deadline_passed(ready_date, at_four) is True


# ---------- per-meal add-on details ----------


def _put_meal(index, meal_id, body):
    return index.handler(
        _event("PUT", f"/meals/{meal_id}", body, path_params={"mealId": meal_id}, claims=ADMIN_CLAIMS), None
    )


def test_meal_stores_its_own_description_and_macros_per_addon(table):
    from backend.lambda_src import index

    large = _create_addon(index, name="Large", description="Default text")
    resp = index.handler(_event("POST", "/meals", {
        "name": "Bowl", "priceCents": 1200,
        "addOnDetails": {large["addOnId"]: {
            "description": "  2 extra oz of chicken  ",
            "macros": {"calories": "750", "proteinG": 60, "carbsG": 70, "fatG": 18},
        }},
    }, claims=ADMIN_CLAIMS), None)
    meal = json.loads(resp["body"])
    assert meal["addOnDetails"] == {large["addOnId"]: {
        "name": "", "priceCents": None,
        "description": "2 extra oz of chicken",
        "macros": {"calories": 750, "proteinG": 60, "carbsG": 70, "fatG": 18},
    }}
    [listed] = json.loads(index.handler(_event("GET", "/meals"), None)["body"])["meals"]
    assert listed["addOnDetails"] == meal["addOnDetails"]


def test_meal_without_addon_details_returns_an_empty_map(table):
    from backend.lambda_src import index

    assert _create_meal(index)["addOnDetails"] == {}


def test_updating_other_meal_fields_keeps_addon_details(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    _put_meal(index, meal["mealId"], {"addOnDetails": {"a1": {"description": "x", "macros": {"calories": 5}}}})
    updated = json.loads(_put_meal(index, meal["mealId"], {"available": False})["body"])
    assert updated["addOnDetails"]["a1"]["macros"]["calories"] == 5


def test_addon_details_must_be_an_object(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    assert _put_meal(index, meal["mealId"], {"addOnDetails": ["a1"]})["statusCode"] == 400


def test_addons_are_listed_alphabetically_by_name(table):
    from backend.lambda_src import index

    for name in ["Large with Cauliflower Rice", "cauliflower Rice", "+ 1oz. Extra Protein", "Large "]:
        _create_addon(index, name=name)
    addons = json.loads(index.handler(_event("GET", "/addons"), None)["body"])["addOns"]
    assert [a["name"] for a in addons] == [
        "+ 1oz. Extra Protein", "cauliflower Rice", "Large", "Large with Cauliflower Rice",
    ]


# ---------- each meal keeps its own copy of the add-on's name and price ----------


def _meal_with_large(index, price_cents=None, addon_price=300):
    large = _create_addon(index, name="Large", price_cents=addon_price)
    meal = _create_meal(index, price_cents=1200)
    detail = {"name": "Large", "description": "Bigger", "macros": {"calories": 700}}
    if price_cents is not None:
        detail["priceCents"] = price_cents
    _put_meal(index, meal["mealId"], {"addOnDetails": {large["addOnId"]: detail}})
    return meal, large


def _order_meal(index, meal, addon):
    return index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [addon["addOnId"]]},
    ]}, claims=CUSTOMER_CLAIMS), None)


def test_meal_saves_the_addon_name_and_its_own_price(table):
    from backend.lambda_src import index

    meal, large = _meal_with_large(index, price_cents=450)
    [listed] = json.loads(index.handler(_event("GET", "/meals"), None)["body"])["meals"]
    assert listed["addOnDetails"][large["addOnId"]] == {
        "name": "Large", "priceCents": 450, "description": "Bigger",
        "macros": {"calories": 700, "proteinG": 0, "carbsG": 0, "fatG": 0},
    }


def test_order_charges_the_meals_price_for_the_addon(table):
    from backend.lambda_src import index

    meal, large = _meal_with_large(index, price_cents=450)
    order = json.loads(_order_meal(index, meal, large)["body"])
    assert order["items"][0]["unitPriceCents"] == 1650
    assert order["items"][0]["selectedAddOns"][0]["priceCents"] == 450


def test_meal_without_its_own_price_uses_the_addons(table):
    from backend.lambda_src import index

    meal, large = _meal_with_large(index)
    [listed] = json.loads(index.handler(_event("GET", "/meals"), None)["body"])["meals"]
    assert listed["addOnDetails"][large["addOnId"]]["priceCents"] is None
    assert json.loads(_order_meal(index, meal, large)["body"])["items"][0]["unitPriceCents"] == 1500


def test_negative_addon_price_on_a_meal_is_rejected(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = _put_meal(index, meal["mealId"], {"addOnDetails": {"a1": {"priceCents": -100}}})
    assert resp["statusCode"] == 400


def test_renaming_an_addon_updates_each_meals_copy_but_not_its_price(table):
    from backend.lambda_src import index

    meal, large = _meal_with_large(index, price_cents=450)
    other = _create_meal(index, name="No add-ons")
    _put_addon(index, large["addOnId"], {"name": "Large Portion", "priceCents": 500})
    meals = {m["mealId"]: m for m in json.loads(index.handler(_event("GET", "/meals"), None)["body"])["meals"]}
    detail = meals[meal["mealId"]]["addOnDetails"][large["addOnId"]]
    assert (detail["name"], detail["priceCents"]) == ("Large Portion", 450)
    assert meals[other["mealId"]]["addOnDetails"] == {}


def test_deleting_an_addon_keeps_the_meals_copy_but_it_cant_be_ordered(table):
    from backend.lambda_src import index

    meal, large = _meal_with_large(index, price_cents=450)
    index.handler(_event("DELETE", f"/addons/{large['addOnId']}", path_params={"addOnId": large["addOnId"]},
                         claims=ADMIN_CLAIMS), None)
    [listed] = json.loads(index.handler(_event("GET", "/meals"), None)["body"])["meals"]
    assert listed["addOnDetails"][large["addOnId"]]["name"] == "Large"
    assert listed["addOnDetails"][large["addOnId"]]["priceCents"] == 450
    assert _order_meal(index, meal, large)["statusCode"] == 400


# ---------- extras & sauces ----------


def _create_extra(index, name="Salsa Verde", price_cents=25, **extra):
    resp = index.handler(
        _event("POST", "/extras", {"name": name, "priceCents": price_cents, **extra}, claims=ADMIN_CLAIMS), None
    )
    assert resp["statusCode"] == 201, resp
    return json.loads(resp["body"])


def _order_with_extras(index, extras, meal=None):
    meal = meal or _create_meal(index, price_cents=1200)
    return index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}], "extras": extras,
    }, claims=CUSTOMER_CLAIMS), None)


def test_extras_are_listed_alphabetically_for_everyone(table):
    from backend.lambda_src import index

    _create_extra(index, name="Sriracha")
    _create_extra(index, name="  bbq Sauce ", price_cents=30)
    extras = json.loads(index.handler(_event("GET", "/extras"), None)["body"])["extras"]
    assert [(e["name"], e["priceCents"], e["available"]) for e in extras] == [
        ("bbq Sauce", 30, True), ("Sriracha", 25, True),
    ]


@pytest.mark.parametrize("body,message", [
    ({"name": " ", "priceCents": 25}, "Extra name is required"),
    ({"name": "Sriracha"}, "Extra price is required"),
    ({"name": "Sriracha", "priceCents": -1}, "Extra price can't be negative"),
])
def test_extra_needs_a_name_and_price(table, body, message):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/extras", body, claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == message


def test_only_admins_manage_extras(table):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/extras", {"name": "Sriracha", "priceCents": 25}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 403


def test_extras_can_be_edited_hidden_and_deleted(table):
    from backend.lambda_src import index

    extra = _create_extra(index)
    path = f"/extras/{extra['extraId']}"
    params = {"extraId": extra["extraId"]}
    resp = index.handler(_event("PUT", path, {"priceCents": 50, "available": False}, path_params=params, claims=ADMIN_CLAIMS), None)
    assert json.loads(resp["body"]) == {**extra, "priceCents": 50, "available": False}

    index.handler(_event("DELETE", path, path_params=params, claims=ADMIN_CLAIMS), None)
    assert json.loads(index.handler(_event("GET", "/extras"), None)["body"])["extras"] == []


def test_order_adds_extras_by_quantity_at_the_servers_price(table):
    from backend.lambda_src import index

    salsa = _create_extra(index, name="Salsa Verde", price_cents=25)
    bbq = _create_extra(index, name="BBQ Sauce", price_cents=25)
    resp = _order_with_extras(index, [
        {"extraId": salsa["extraId"], "quantity": 2, "priceCents": 1},  # client price ignored
        {"extraId": bbq["extraId"], "quantity": 3},
    ])

    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["extras"] == [
        {"extraId": salsa["extraId"], "name": "Salsa Verde", "priceCents": 25, "quantity": 2,
         "description": "", "macros": NO_MACROS},
        {"extraId": bbq["extraId"], "name": "BBQ Sauce", "priceCents": 25, "quantity": 3,
         "description": "", "macros": NO_MACROS},
    ]
    assert order["totalCents"] == 1200 + 5 * 25
    # Saved with the order (a later price change doesn't touch it).
    [listed] = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])["orders"]
    assert listed["extras"] == order["extras"]


def test_extras_with_zero_quantity_are_dropped(table):
    from backend.lambda_src import index

    salsa = _create_extra(index)
    order = json.loads(_order_with_extras(index, [{"extraId": salsa["extraId"], "quantity": 0}])["body"])
    assert order["extras"] == [] and order["totalCents"] == 1200


def test_hidden_or_unknown_extras_cant_be_ordered(table):
    from backend.lambda_src import index

    hidden = _create_extra(index, available=False)
    for extra_id in (hidden["extraId"], "nope"):
        resp = _order_with_extras(index, [{"extraId": extra_id, "quantity": 1}])
        assert resp["statusCode"] == 400
        assert "no longer available" in json.loads(resp["body"])["message"]


@pytest.mark.parametrize("quantity", [-1, "two"])
def test_extra_quantity_must_be_a_whole_number(table, quantity):
    from backend.lambda_src import index

    salsa = _create_extra(index)
    assert _order_with_extras(index, [{"extraId": salsa["extraId"], "quantity": quantity}])["statusCode"] == 400


def test_extras_alone_are_not_an_order(table):
    from backend.lambda_src import index

    salsa = _create_extra(index)
    resp = index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "items": [], "extras": [{"extraId": salsa["extraId"], "quantity": 2}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["message"] == "order must include at least one meal"


def test_orders_from_before_extras_list_none(table):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    assert order["extras"] == []


NO_MACROS = {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0}


def test_extra_has_an_optional_description_and_macros(table):
    from backend.lambda_src import index

    plain = _create_extra(index, name="Sriracha")
    assert (plain["description"], plain["macros"]) == ("", NO_MACROS)

    salsa = _create_extra(index, description="  Tangy tomatillo salsa, 2 oz. ",
                          macros={"calories": "20", "proteinG": 0, "carbsG": 4, "fatG": 0})
    assert salsa["description"] == "Tangy tomatillo salsa, 2 oz."
    assert salsa["macros"] == {"calories": 20, "proteinG": 0, "carbsG": 4, "fatG": 0}

    # Editing just the price keeps them; sending them changes them.
    path, params = f"/extras/{salsa['extraId']}", {"extraId": salsa["extraId"]}
    kept = json.loads(index.handler(_event("PUT", path, {"priceCents": 30}, path_params=params, claims=ADMIN_CLAIMS), None)["body"])
    assert (kept["description"], kept["macros"]["calories"]) == ("Tangy tomatillo salsa, 2 oz.", 20)
    changed = json.loads(index.handler(_event("PUT", path, {"description": "", "macros": {"calories": 25}},
                                              path_params=params, claims=ADMIN_CLAIMS), None)["body"])
    assert changed["description"] == "" and changed["macros"] == {**NO_MACROS, "calories": 25}


def test_order_saves_each_extras_description_and_macros(table):
    from backend.lambda_src import index

    salsa = _create_extra(index, description="2 oz.", macros={"calories": 20, "carbsG": 4})
    order = json.loads(_order_with_extras(index, [{"extraId": salsa["extraId"], "quantity": 2}])["body"])
    [saved] = order["extras"]
    assert saved["description"] == "2 oz." and saved["macros"]["calories"] == 20

    # Changing the extra later doesn't change the order.
    index.handler(_event("PUT", f"/extras/{salsa['extraId']}", {"description": "4 oz."},
                         path_params={"extraId": salsa["extraId"]}, claims=ADMIN_CLAIMS), None)
    [listed] = json.loads(index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)["body"])["orders"]
    assert listed["extras"][0]["description"] == "2 oz."


# ---------- order emails to the admin ----------

@pytest.fixture
def outbox(monkeypatch):
    """Every email sent, to anyone."""
    from backend.lambda_src import index

    sent = []
    monkeypatch.setattr(index, "FROM_EMAIL", "GTX Meals <menu@example.com>")
    monkeypatch.setattr(index, "SITE_URL", "https://example.com")
    monkeypatch.setattr(index, "_admin_emails", lambda: ["admin@example.com"])
    monkeypatch.setattr(index, "_admin_usernames", lambda: {"admin-1"})
    monkeypatch.setattr(index.ses, "send_email", lambda **kwargs: sent.append(kwargs))
    return sent


@pytest.fixture
def admin_inbox(outbox):
    """The emails sent to the admin - a live view of the outbox."""
    class Inbox:
        def _items(self):
            return [e for e in outbox if e["Destination"]["ToAddresses"] == ["admin@example.com"]]

        def __iter__(self):
            return iter(self._items())

        def __getitem__(self, i):
            return self._items()[i]

        def __len__(self):
            return len(self._items())

    return Inbox()


def _cancel_by_id(index, order_id, body=None):
    return index.handler(
        _event("PUT", f"/orders/{order_id}", body or {"status": "cancelled"},
               path_params={"orderId": order_id}, claims=CUSTOMER_CLAIMS),
        None,
    )


def test_admin_is_emailed_when_an_order_is_placed(table, admin_inbox):
    from backend.lambda_src import index

    meal = _create_meal(index, name="Smash Burger Tacos", price_cents=1200)
    resp = index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 2, "note": "no onions"},
    ]}, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 201

    [email] = admin_inbox
    assert email["Destination"] == {"ToAddresses": ["admin@example.com"]}
    assert email["Message"]["Subject"]["Data"] == "New Meal Order Received!"
    text = email["Message"]["Body"]["Text"]["Data"]
    assert text.startswith("Casey Customer placed an order for ")
    assert "2 x Smash Burger Tacos - note: no onions" in text
    assert "Total: $24.00" in text
    assert f"Phone: {PHONE}" in text
    assert "https://example.com/admin-orders.html" in text


def test_admin_is_emailed_when_an_order_is_cancelled(table, admin_inbox):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    assert _cancel_by_id(index, order["orderId"])["statusCode"] == 200
    assert [e["Message"]["Subject"]["Data"] for e in admin_inbox] == ["New Meal Order Received!", "Meal Order Cancelled"]
    assert admin_inbox[-1]["Message"]["Body"]["Text"]["Data"].startswith("Casey Customer cancelled their order")


def test_an_edited_order_sends_one_updated_email(table, admin_inbox):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    _cancel_by_id(index, order["orderId"], {"status": "cancelled", "forEdit": True})
    resp = index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "replacesOrderId": order["orderId"],
        "items": [{"mealId": order["items"][0]["mealId"], "quantity": 3}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 201
    assert [e["Message"]["Subject"]["Data"] for e in admin_inbox] == ["New Meal Order Received!", "Meal Order Updated"]
    assert admin_inbox[-1]["Message"]["Body"]["Text"]["Data"].startswith("Casey Customer updated their order")


def test_replacing_someone_elses_order_is_just_a_new_order(table, admin_inbox):
    from backend.lambda_src import index

    meal = _create_meal(index)
    index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "replacesOrderId": "not-mine",
        "items": [{"mealId": meal["mealId"], "quantity": 1}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert admin_inbox[-1]["Message"]["Subject"]["Data"] == "New Meal Order Received!"


def test_a_failed_admin_email_does_not_fail_the_order(table, monkeypatch, capsys):
    from backend.lambda_src import index

    def boom(**kwargs):
        raise RuntimeError("SES is down")

    monkeypatch.setattr(index, "FROM_EMAIL", "GTX Meals <menu@example.com>")
    monkeypatch.setattr(index, "_admin_emails", lambda: ["admin@example.com"])
    monkeypatch.setattr(index.ses, "send_email", boom)
    order = _place_order(index, CUSTOMER_CLAIMS)
    assert order["status"] == "placed"
    assert "ERROR emailing the admin about order" in capsys.readouterr().out


# ---------- messages to customers ----------

def _to(outbox, address):
    return [e for e in outbox if e["Destination"]["ToAddresses"] == [address]]


def _text(email):
    return email["Message"]["Body"]["Text"]["Data"]


def _subject(email):
    return email["Message"]["Subject"]["Data"]


def _put_profile(index, user_id, **fields):
    index.table.put_item(Item={"PK": f"USER#{user_id}", "SK": "PROFILE", "preferredContact": "email",
                               "unsubscribed": False, **fields})


def test_customer_gets_an_order_confirmation(table, outbox):
    from backend.lambda_src import index

    meal = _create_meal(index, name="Chicken Enchilada Bowl", price_cents=1000)
    large = _create_addon(index, name="Large", price_cents=300)
    _offer_addons(index, meal["mealId"], large["addOnId"])
    index.handler(_event("POST", "/orders", {"contactPhone": PHONE, "items": [
        {"mealId": meal["mealId"], "quantity": 2, "selectedAddOnIds": [large["addOnId"]], "note": "no onions"},
    ]}, claims=CUSTOMER_CLAIMS), None)

    [email] = _to(outbox, "customer@example.com")  # the login email - no profile email saved
    assert _subject(email) == "Thank You for Your Order! | The Gym TX Meals"
    assert email["ReplyToAddresses"] == ["admin@example.com"]
    text = _text(email)
    assert text.startswith("Hi Casey!\n\nThank you for ordering from The Gym TX Meals!")
    assert "Your Order:\n2 x Chicken Enchilada Bowl (Large) - $26.00\n    Note: no onions\n" in text
    assert "Order Total: $26.00\nPickup Date: Monday, October 26\n" in text
    assert ("Pickup Location:\nThe Gym TX\n4112 Williams Drive, Suite 105\nGeorgetown, TX 78628\n\n"
            "Pickup Hours:\n8:00 AM–1:00 PM or 3:30–6:30 PM") in text
    assert "Elgin" not in text
    assert "Venmo to @lgraffeo88" in text
    assert text.endswith("Christian & Liz\nThe Gym TX Meals")
    assert "nsubscribe" not in text  # about their own order, not a marketing email


def test_order_confirmation_goes_to_the_profile_email(table, outbox):
    from backend.lambda_src import index

    _put_profile(index, "user-1", name="Casey Customer", email="casey@home.example")
    _place_order(index, CUSTOMER_CLAIMS)
    assert len(_to(outbox, "casey@home.example")) == 1
    assert _to(outbox, "customer@example.com") == []


def test_customer_gets_an_updated_confirmation_after_an_edit(table, outbox):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    _cancel_by_id(index, order["orderId"], {"status": "cancelled", "forEdit": True})
    # Cancelled to be edited: no cancellation email.
    assert [_subject(e) for e in _to(outbox, "customer@example.com")] == [
        "Thank You for Your Order! | The Gym TX Meals",
    ]
    index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "replacesOrderId": order["orderId"],
        "items": [{"mealId": order["items"][0]["mealId"], "quantity": 3}],
    }, claims=CUSTOMER_CLAIMS), None)

    updated = _to(outbox, "customer@example.com")[-1]
    assert _subject(updated) == "Your Order Has Been Updated! | The Gym TX Meals"
    assert "Your Updated Order:\n3 x Chicken Bowl - $36.00\n\nUpdated Total: $36.00\n" in _text(updated)


def test_customer_gets_a_cancellation_email(table, outbox):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS, quantity=2)
    _cancel_by_id(index, order["orderId"])

    cancelled = _to(outbox, "customer@example.com")[-1]
    assert _subject(cancelled) == "Your Order Has Been Canceled | The Gym TX Meals"
    text = _text(cancelled)
    assert "Canceled Order:\n2 x Chicken Bowl\n\nOrder Total: $24.00\n" in text
    assert "contact us at gtxmeals@gmail.com" in text


def test_no_customer_email_when_the_change_is_refused(table, outbox, monkeypatch):
    from backend.lambda_src import index

    order = _order_for_monday(index)
    sent_before = len(outbox)
    _at(monkeypatch, "2026-10-23T21:00:00+00:00")  # past the Friday deadline
    assert _cancel_by_id(index, order["orderId"])["statusCode"] == 400
    assert len(outbox) == sent_before


def test_text_preferring_customer_also_gets_the_short_text(table, outbox, monkeypatch):
    from backend.lambda_src import index

    texts = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: texts.append((phone, message)))
    _put_profile(index, "user-1", name="Casey Customer", phone="(555) 555-1234", preferredContact="text")
    index.table.put_item(Item={**index.SETTINGS_KEY, "smsEnabled": True})
    _place_order(index, CUSTOMER_CLAIMS)

    assert len(_to(outbox, "customer@example.com")) == 1  # the full confirmation still goes by email
    [(phone, message)] = texts
    assert phone == "(555) 555-1234"
    assert message == (
        "Hi Casey! Thanks for ordering from The Gym TX Meals! Your order total is $12.00. Payment is due "
        "when ordering (Venmo: @lgraffeo88 or cash by arrangement). Pickup is Monday, October 26 at "
        "The Gym TX, Georgetown. "
        "See your confirmation email for details!"
    )


def test_no_text_while_texting_is_switched_off(table, outbox, monkeypatch):
    from backend.lambda_src import index

    texts = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: texts.append(message))
    _put_profile(index, "user-1", name="Casey", phone="(555) 555-1234", preferredContact="text")
    _place_order(index, CUSTOMER_CLAIMS)
    assert texts == []
    assert len(_to(outbox, "customer@example.com")) == 1


def test_a_failed_customer_email_does_not_fail_the_order(table, monkeypatch, capsys):
    from backend.lambda_src import index

    def boom(**kwargs):
        raise RuntimeError("SES is down")

    monkeypatch.setattr(index, "FROM_EMAIL", "GTX Meals <menu@example.com>")
    monkeypatch.setattr(index, "_admin_emails", lambda: ["admin@example.com"])
    monkeypatch.setattr(index.ses, "send_email", boom)
    assert _place_order(index, CUSTOMER_CLAIMS)["status"] == "placed"
    assert "ERROR sending the placed message for order" in capsys.readouterr().out


# ---------- scheduled reminders ----------

THURSDAY_6PM = "2026-10-22T23:00:00+00:00"  # 6 PM CDT
PICKUP_7AM = "2026-10-26T12:00:00+00:00"  # Monday 7 AM CDT


def _customers_for_reminders(index):
    _put_profile(index, "user-1", name="Casey Customer", email="casey@example.com")  # orders below
    _put_profile(index, "u-dana", name="Dana Diaz", email="dana@example.com")
    _put_profile(index, "u-ellis", name="Ellis", email="ellis@example.com", unsubscribed=True)
    _put_profile(index, "u-flo", name="Flo", email="flo@example.com", phone="5555550000", preferredContact="text")
    _put_profile(index, "admin-1", name="Admin", email="admin@example.com")


def test_thursday_reminder_goes_to_opted_in_customers_who_have_not_ordered(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _customers_for_reminders(index)
    _place_order(index, CUSTOMER_CLAIMS)
    outbox.clear()
    _at(monkeypatch, THURSDAY_6PM)
    result = index.handler({"job": "ordering-reminder"}, None)

    # Flo prefers text, but texting is off - so email instead.
    assert sorted(e["Destination"]["ToAddresses"][0] for e in outbox) == ["dana@example.com", "flo@example.com"]
    dana = _to(outbox, "dana@example.com")[0]
    assert _subject(dana) == "Don't Forget to Order Your Meals! | The Gym TX Meals"
    assert _text(dana).startswith("Hi Dana!\n\nJust a friendly reminder that our weekly meal prep orders close "
                                  "tomorrow (Friday) at 2:00 PM")
    assert result == {"sent": 2, "alreadySent": 0, "failed": 0}


def test_thursday_reminder_has_an_unsubscribe_link(table, outbox, monkeypatch):
    from backend.lambda_src import index

    monkeypatch.setattr(index, "_with_unsubscribe_footer", lambda message, email: f"{message}\n[unsubscribe {email}]")
    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, THURSDAY_6PM)
    index.handler({"job": "ordering-reminder"}, None)
    assert _text(outbox[0]).endswith("[unsubscribe dana@example.com]")


def test_thursday_reminder_by_text_when_texting_is_on(table, outbox, monkeypatch):
    from backend.lambda_src import index

    texts = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: texts.append((phone, message)))
    index.table.put_item(Item={**index.SETTINGS_KEY, "smsEnabled": True})
    _put_profile(index, "u-flo", name="Flo Fern", email="flo@example.com", phone="5555550000", preferredContact="text")
    _at(monkeypatch, THURSDAY_6PM)
    index.handler({"job": "ordering-reminder"}, None)

    assert outbox == []
    assert texts == [("5555550000", "Hi Flo! Don't forget to order your meals for next week! Orders close tomorrow "
                                    "at 2 PM or when spots fill. Order at www.gtxmeals.com")]


def test_a_retried_run_does_not_send_twice(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, THURSDAY_6PM)
    index.handler({"job": "ordering-reminder"}, None)
    again = index.handler({"job": "ordering-reminder"}, None)
    assert len(outbox) == 1
    assert again == {"sent": 0, "alreadySent": 1, "failed": 0}


def test_a_failed_send_is_retried_next_run(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, THURSDAY_6PM)
    real_send = index.ses.send_email
    monkeypatch.setattr(index.ses, "send_email", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("blip")))
    assert index.handler({"job": "ordering-reminder"}, None)["failed"] == 1
    monkeypatch.setattr(index.ses, "send_email", real_send)
    assert index.handler({"job": "ordering-reminder"}, None)["sent"] == 1


def test_reminders_only_run_in_their_central_hour(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, "2026-10-23T00:00:00+00:00")  # the other UTC firing: 7 PM CDT
    assert index.handler({"job": "ordering-reminder"}, None) == {"skipped": "not Thursday 6 PM Central"}
    _at(monkeypatch, "2026-10-26T13:00:00+00:00")  # 8 AM CDT
    assert index.handler({"job": "pickup-reminder"}, None) == {"skipped": "not 7 AM Central"}
    assert outbox == []


def test_reminder_hours_follow_standard_time_too(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, "2026-11-13T00:00:00+00:00")  # Thursday Nov 12, 6 PM CST
    assert index.handler({"job": "ordering-reminder"}, None)["sent"] == 1


def test_no_thursday_reminder_when_ordering_is_closed(table, outbox, monkeypatch):
    from backend.lambda_src import index

    index.table.put_item(Item={**index.SETTINGS_KEY, "orderingMode": "closed"})
    _put_profile(index, "u-dana", name="Dana", email="dana@example.com")
    _at(monkeypatch, THURSDAY_6PM)
    assert index.handler({"job": "ordering-reminder"}, None) == {"skipped": "ordering is closed"}
    assert outbox == []


def test_pickup_reminder_goes_once_to_each_customer_with_an_order_today(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _customers_for_reminders(index)
    _place_order(index, CUSTOMER_CLAIMS)
    _place_order(index, CUSTOMER_CLAIMS)  # two orders - still one reminder
    dana = {"sub": "u-dana", "email": "dana@example.com", "name": "Dana Diaz"}
    cancelled = _place_order(index, dana)
    index.handler(_event("PUT", f"/orders/{cancelled['orderId']}", {"status": "cancelled"},
                         path_params={"orderId": cancelled["orderId"]}, claims=dana), None)
    outbox.clear()
    _at(monkeypatch, PICKUP_7AM)
    result = index.handler({"job": "pickup-reminder"}, None)

    [email] = outbox
    assert email["Destination"]["ToAddresses"] == ["casey@example.com"]
    assert _subject(email) == "Your Meals Are Ready for Pickup! | The Gym TX Meals"
    assert _text(email).startswith("Good morning, Casey!\n\nYour meals are ready for pickup today! 🎉")
    assert "Pickup Hours:\n8:00 AM–1:00 PM or 3:30–6:30 PM" in _text(email)
    assert result["sent"] == 1


def test_no_pickup_reminder_on_a_day_without_pickups(table, outbox, monkeypatch):
    from backend.lambda_src import index

    _place_order(index, CUSTOMER_CLAIMS)  # for Monday the 26th
    _at(monkeypatch, "2026-10-27T12:00:00+00:00")  # Tuesday 7 AM
    assert index.handler({"job": "pickup-reminder"}, None)["sent"] == 0
    assert _to(outbox, "customer@example.com")[-1]["Message"]["Subject"]["Data"].startswith("Thank You")


def test_order_stores_meal_suggestions(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "mealSuggestions": "  Beef enchiladas & lasagna!  " + "x" * 2000,
        "items": [{"mealId": meal["mealId"], "quantity": 1}],
    }, claims=CUSTOMER_CLAIMS), None)
    suggestions = json.loads(resp["body"])["mealSuggestions"]
    assert suggestions.startswith("Beef enchiladas & lasagna!")
    assert len(suggestions) == index.MEAL_SUGGESTIONS_MAX
    admin = json.loads(index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS), None)["body"])
    assert admin["orders"][0]["mealSuggestions"] == suggestions


def test_meal_suggestions_are_optional(table):
    from backend.lambda_src import index

    assert _place_order(index, CUSTOMER_CLAIMS)["mealSuggestions"] == ""


def _admin_set_status(index, sub, order_id, status):
    return index.handler(_event("PUT", f"/admin/orders/{sub}/{order_id}", {"status": status},
                                path_params={"sub": sub, "orderId": order_id}, claims=ADMIN_CLAIMS), None)


def test_customer_is_emailed_when_the_admin_cancels_their_order(table, outbox):
    from backend.lambda_src import index

    _put_profile(index, "user-1", name="Casey Customer", email="casey@example.com")
    order = _place_order(index, CUSTOMER_CLAIMS, quantity=2)
    outbox.clear()
    assert _admin_set_status(index, "user-1", order["orderId"], "cancelled")["statusCode"] == 200

    [email] = outbox  # to the customer only - the admin did it
    assert email["Destination"]["ToAddresses"] == ["casey@example.com"]
    assert _subject(email) == "Your Order Has Been Canceled | The Gym TX Meals"
    assert "Canceled Order:\n2 x Chicken Bowl\n" in _text(email)


def test_admin_cancel_email_falls_back_to_the_login_email(table, outbox, monkeypatch):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    outbox.clear()
    monkeypatch.setattr(index.cognito_idp, "admin_get_user", lambda **kwargs: {
        "UserAttributes": [{"Name": "email", "Value": "login@example.com"}],
    })
    _admin_set_status(index, "user-1", order["orderId"], "cancelled")
    assert [e["Destination"]["ToAddresses"] for e in outbox] == [["login@example.com"]]


def test_other_admin_status_changes_send_no_email(table, outbox):
    from backend.lambda_src import index

    _put_profile(index, "user-1", name="Casey", email="casey@example.com")
    order = _place_order(index, CUSTOMER_CLAIMS)
    outbox.clear()
    _admin_set_status(index, "user-1", order["orderId"], "prepared")
    _admin_set_status(index, "user-1", order["orderId"], "cancelled")
    _admin_set_status(index, "user-1", order["orderId"], "cancelled")  # already cancelled - not again
    assert [_subject(e) for e in outbox] == ["Your Order Has Been Canceled | The Gym TX Meals"]



# ---------- pickup location (admin-set) ----------

def _admin_put_customer(index, username, body):
    return index.handler(_event("PUT", f"/customers/{username}", body,
                                path_params={"username": username}, claims=ADMIN_CLAIMS), None)


@pytest.fixture
def customer_admin(monkeypatch):
    from backend.lambda_src import index

    monkeypatch.setattr(index, "_resolve_sub", lambda username: username)
    monkeypatch.setattr(index, "_admin_usernames", lambda: {"admin-1"})


def test_admin_sets_a_pickup_location_the_customer_can_see(table, customer_admin):
    from backend.lambda_src import index

    resp = _admin_put_customer(index, "user-1", {"name": "Casey", "email": "c@example.com",
                                                  "pickupLocation": "  Elgin  "})
    assert json.loads(resp["body"])["pickupLocation"] == "Elgin"
    profile = json.loads(index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)["body"])
    assert profile["pickupLocation"] == "Elgin"


def test_customer_cannot_change_their_pickup_location(table, customer_admin):
    from backend.lambda_src import index

    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "c@example.com", "pickupLocation": "Elgin"})
    resp = index.handler(_event("PUT", "/profile", {"phone": "5555550000", "pickupLocation": "Austin"},
                                claims=CUSTOMER_CLAIMS), None)
    assert json.loads(resp["body"])["pickupLocation"] == "Elgin"
    stored = index.table.get_item(Key={"PK": "USER#user-1", "SK": "PROFILE"})["Item"]
    assert (stored["pickupLocation"], stored["phone"]) == ("Elgin", "5555550000")


def test_admin_edit_without_the_field_keeps_the_pickup_location(table, customer_admin):
    from backend.lambda_src import index

    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "c@example.com", "pickupLocation": "Elgin"})
    _admin_put_customer(index, "user-1", {"name": "Casey C", "email": "c@example.com"})
    stored = index.table.get_item(Key={"PK": "USER#user-1", "SK": "PROFILE"})["Item"]
    assert stored["pickupLocation"] == "Elgin"


def test_order_records_the_customers_pickup_location(table, customer_admin):
    from backend.lambda_src import index

    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "c@example.com", "pickupLocation": "Elgin"})
    order = _place_order(index, CUSTOMER_CLAIMS)
    assert order["pickupLocation"] == "Elgin"
    admin = json.loads(index.handler(_event("GET", "/admin/orders", claims=ADMIN_CLAIMS), None)["body"])
    assert admin["orders"][0]["pickupLocation"] == "Elgin"



def test_emails_use_the_customers_own_pickup_location(table, outbox, customer_admin):
    from backend.lambda_src import index

    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "casey@example.com", "pickupLocation": "Elgin"})
    order = _place_order(index, CUSTOMER_CLAIMS)
    confirmation = _text(_to(outbox, "casey@example.com")[-1])
    assert "Pickup Location:\nElgin\n\nPlease follow your confirmed pickup instructions." in confirmation
    assert "Williams Drive" not in confirmation
    assert "Pickup location: Elgin" in _text(_to(outbox, "admin@example.com")[-1])

    _cancel_by_id(index, order["orderId"], {"status": "cancelled", "forEdit": True})
    index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "replacesOrderId": order["orderId"],
        "items": [{"mealId": order["items"][0]["mealId"], "quantity": 2}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert "Pickup Date: Monday, October 26\nPickup Location: Elgin\n" in _text(_to(outbox, "casey@example.com")[-1])


def test_updated_email_shows_the_default_location(table, outbox):
    from backend.lambda_src import index

    order = _place_order(index, CUSTOMER_CLAIMS)
    _cancel_by_id(index, order["orderId"], {"status": "cancelled", "forEdit": True})
    index.handler(_event("POST", "/orders", {
        "contactPhone": PHONE, "replacesOrderId": order["orderId"],
        "items": [{"mealId": order["items"][0]["mealId"], "quantity": 1}],
    }, claims=CUSTOMER_CLAIMS), None)
    assert "Pickup Location: The Gym TX, Georgetown\n" in _text(_to(outbox, "customer@example.com")[-1])


def test_pickup_reminder_uses_the_stored_location(table, outbox, monkeypatch, customer_admin):
    from backend.lambda_src import index

    _place_order(index, CUSTOMER_CLAIMS)
    # Set after the order was placed - the reminder uses what's stored now.
    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "casey@example.com", "pickupLocation": "Elgin"})
    outbox.clear()
    _at(monkeypatch, PICKUP_7AM)
    index.handler({"job": "pickup-reminder"}, None)

    text = _text(outbox[0])
    assert "Pickup Location:\nElgin\n\nPlease follow your confirmed pickup instructions." in text
    assert "Williams Drive" not in text and "Pickup Hours" not in text


def test_pickup_reminder_text_uses_the_stored_location(table, outbox, monkeypatch, customer_admin):
    from backend.lambda_src import index

    texts = []
    monkeypatch.setattr(index, "_send_text_via_twilio", lambda phone, message: texts.append(message))
    index.table.put_item(Item={**index.SETTINGS_KEY, "smsEnabled": True})
    _admin_put_customer(index, "user-1", {"name": "Casey", "email": "casey@example.com", "phone": "5555550000",
                                           "preferredContact": "text", "pickupLocation": "Elgin"})
    _place_order(index, CUSTOMER_CLAIMS)
    texts.clear()
    _at(monkeypatch, PICKUP_7AM)
    index.handler({"job": "pickup-reminder"}, None)
    assert texts == ["Good morning, Casey! Your Gym TX Meals are ready for pickup today at Elgin. See you soon!"]
