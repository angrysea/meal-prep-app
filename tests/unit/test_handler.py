import json
import os
from datetime import date, timedelta

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


def test_create_addon_with_macros(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event(
            "POST", "/addons",
            {
                "name": "Cauliflower Rice",
                "priceCents": 100,
                "macros": {"calories": 25, "proteinG": 1, "carbsG": 5, "fatG": 0},
            },
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    addon = json.loads(resp["body"])
    assert addon["macros"] == {"calories": 25, "proteinG": 1, "carbsG": 5, "fatG": 0}


def test_create_addon_defaults_macros_when_omitted(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    assert addon["macros"] == {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0}


def test_admin_can_update_addon_macros(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    resp = index.handler(
        _event(
            "PUT", f"/addons/{addon['addOnId']}",
            {"macros": {"calories": 60, "proteinG": 7, "carbsG": 0, "fatG": 0}},
            path_params={"addOnId": addon["addOnId"]}, claims=ADMIN_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["macros"] == {"calories": 60, "proteinG": 7, "carbsG": 0, "fatG": 0}


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


def test_get_settings_defaults_to_empty_ready_date_and_sms_off(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/settings"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"nextReadyDate": "", "smsEnabled": False, "contactEmail": ""}


def test_update_settings_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01"}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 403


def test_admin_can_update_settings(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01", "smsEnabled": True}, claims=ADMIN_CLAIMS), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"nextReadyDate": "2026-12-01", "smsEnabled": True}

    get_resp = index.handler(_event("GET", "/settings"), None)
    assert json.loads(get_resp["body"]) == {"nextReadyDate": "2026-12-01", "smsEnabled": True, "contactEmail": ""}


def test_get_settings_reports_the_admin_email_as_contact_email(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")

    resp = index.handler(_event("GET", "/settings"), None)
    assert json.loads(resp["body"])["contactEmail"] == "admin@example.com"


def test_update_settings_defaults_sms_enabled_to_false_when_omitted(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01"}, claims=ADMIN_CLAIMS), None
    )
    assert json.loads(resp["body"])["smsEnabled"] is False


def test_update_settings_merges_instead_of_overwriting(table):
    from backend.lambda_src import index

    index.handler(_event("PUT", "/settings", {"smsEnabled": True}, claims=ADMIN_CLAIMS), None)

    # Saving just the ready date (as the Builder page's ready-date form
    # does) must not silently reset smsEnabled back to False.
    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01"}, claims=ADMIN_CLAIMS), None
    )
    assert json.loads(resp["body"]) == {"nextReadyDate": "2026-12-01", "smsEnabled": True}

    # And saving just smsEnabled must not reset nextReadyDate either.
    resp = index.handler(_event("PUT", "/settings", {"smsEnabled": False}, claims=ADMIN_CLAIMS), None)
    assert json.loads(resp["body"]) == {"nextReadyDate": "2026-12-01", "smsEnabled": False}


def test_update_settings_rejects_bad_date_format(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "12/01/2026"}, claims=ADMIN_CLAIMS), None
    )
    assert resp["statusCode"] == 400


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


def test_create_order_snapshots_addon_macros(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    resp = index.handler(
        _event(
            "POST", "/addons",
            {"name": "Cauliflower Rice", "priceCents": 100,
             "macros": {"calories": 25, "proteinG": 1, "carbsG": 5, "fatG": 0}},
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    cauli = json.loads(resp["body"])

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
        "calories": 25, "proteinG": 1, "carbsG": 5, "fatG": 0,
    }

    # Changing the add-on's macros afterward must not rewrite the past order.
    index.handler(
        _event(
            "PUT", f"/addons/{cauli['addOnId']}",
            {"macros": {"calories": 999, "proteinG": 0, "carbsG": 0, "fatG": 0}},
            path_params={"addOnId": cauli["addOnId"]}, claims=ADMIN_CLAIMS,
        ),
        None,
    )
    orders_resp = index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)
    stored_order = json.loads(orders_resp["body"])["orders"][0]
    assert stored_order["items"][0]["selectedAddOns"][0]["macros"]["calories"] == 25


def test_create_order_sums_multiple_addons(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, description="Large", price_cents=300)
    extra_protein = _create_addon(index, description="Extra Protein", price_cents=150)

    resp = index.handler(
        _event(
            "POST", "/orders",
            {"contactPhone": PHONE, "items": [{
                "mealId": meal["mealId"], "quantity": 2,
                "selectedAddOnIds": [large["addOnId"], extra_protein["addOnId"]],
            }]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    # unit price: 1200 + 300 + 150 = 1650, times quantity 2
    assert order["totalCents"] == 3300
    assert order["items"][0]["unitPriceCents"] == 1650


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


def test_create_order_stores_the_current_ready_date(table):
    from backend.lambda_src import index

    index.handler(_event("PUT", "/settings", {"nextReadyDate": "2026-12-25"}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["readyDate"] == "2026-12-25"


def test_create_order_allowed_when_no_ready_date_is_set(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["readyDate"] == ""


def test_create_order_allowed_exactly_at_the_cutoff(table):
    from backend.lambda_src import index

    ready_date = (date.today() + timedelta(days=2)).isoformat()
    index.handler(_event("PUT", "/settings", {"nextReadyDate": ready_date}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201


def test_create_order_rejected_inside_the_cutoff(table):
    from backend.lambda_src import index

    ready_date = (date.today() + timedelta(days=1)).isoformat()
    index.handler(_event("PUT", "/settings", {"nextReadyDate": ready_date}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_rejected_after_the_ready_date_has_passed(table):
    from backend.lambda_src import index

    ready_date = (date.today() - timedelta(days=1)).isoformat()
    index.handler(_event("PUT", "/settings", {"nextReadyDate": ready_date}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"contactPhone": PHONE, "items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


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
        "preferredContact": "email", "unsubscribed": False,
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
        "preferredContact": "text", "unsubscribed": True,
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
        "preferredContact": "text", "unsubscribed": True,
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


def test_reminders_counts_failure_when_contact_detail_missing(table):
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
