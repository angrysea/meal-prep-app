import json
import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("TABLE_NAME", "MealPrepTable")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ADMINS_GROUP_NAME", "Admins")


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


def _event(method, path, body=None, path_params=None, claims=None):
    event = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "body": json.dumps(body) if body is not None else None,
    }
    if path_params:
        event["pathParameters"] = path_params
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return event


CUSTOMER_CLAIMS = {"sub": "user-1", "email": "customer@example.com"}
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


def _create_addon(index, description="Large", price_cents=300):
    resp = index.handler(
        _event("POST", "/addons", {"description": description, "priceCents": price_cents}, claims=ADMIN_CLAIMS),
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
        _event("POST", "/addons", {"description": "Large", "priceCents": 300}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 403


def test_admin_can_create_and_delete_addon(table):
    from backend.lambda_src import index

    addon = _create_addon(index)
    assert addon["description"] == "Large"
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
            {"items": [{"mealId": meal["mealId"], "quantity": 2, "priceCents": 1}],
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
            {"items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": [large["addOnId"]]}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 1500  # 1200 base + 300 for Large
    assert order["items"][0]["selectedAddOns"] == [
        {"addOnId": large["addOnId"], "description": "Large", "priceCents": 300}
    ]


def test_create_order_sums_multiple_addons(table):
    from backend.lambda_src import index

    meal = _create_meal(index, price_cents=1200)
    large = _create_addon(index, description="Large", price_cents=300)
    extra_protein = _create_addon(index, description="Extra Protein", price_cents=150)

    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
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
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 1200
    assert order["items"][0]["selectedAddOns"] == []


def test_create_order_fails_for_unknown_addon(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{"mealId": meal["mealId"], "quantity": 1, "selectedAddOnIds": ["does-not-exist"]}]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_rejects_unknown_meal(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": "does-not-exist", "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_list_orders_is_scoped_to_the_caller(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    other_claims = {"sub": "user-2", "email": "other@example.com"}
    index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 3}]}, claims=other_claims),
        None,
    )

    resp = index.handler(_event("GET", "/orders", claims=CUSTOMER_CLAIMS), None)
    orders = json.loads(resp["body"])["orders"]
    assert len(orders) == 1
    assert orders[0]["items"][0]["quantity"] == 1


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

    resp = index.handler(_event("GET", "/profile", claims=CUSTOMER_CLAIMS), None)
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


def test_create_order_stores_delivery_contact_details(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {
                "items": [{"mealId": meal["mealId"], "quantity": 1}],
                "deliveryName": "Ada Lovelace",
                "deliveryEmail": "ada@example.com",
                "deliveryPhone": "555-1234",
                "deliveryAddress": "123 Main St",
            },
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    order = json.loads(resp["body"])
    assert order["deliveryEmail"] == "ada@example.com"
    assert order["deliveryPhone"] == "555-1234"


def _save_profile(index, claims, **fields):
    body = {"preferredContact": "email", "unsubscribed": False}
    body.update(fields)
    index.handler(_event("PUT", "/profile", body, claims=claims), None)


def test_reminders_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(_event("POST", "/reminders/send", claims=CUSTOMER_CLAIMS), None)
    assert resp["statusCode"] == 403


def test_reminders_sends_by_preference_and_skips_unsubscribed(table):
    import boto3
    from backend.lambda_src import index

    # moto's SES mock enforces the same verified-sender rule real SES does.
    boto3.client("ses", region_name="us-east-1").verify_email_identity(EmailAddress=index.FROM_EMAIL)

    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, email="u1@example.com", preferredContact="email")
    _save_profile(index, {"sub": "u2", "email": "u2@example.com"}, phone="+15551234567", preferredContact="text")
    _save_profile(index, {"sub": "u3", "email": "u3@example.com"}, email="u3@example.com", unsubscribed=True)

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 200
    result = json.loads(resp["body"])
    assert result["sentEmail"] == 1
    assert result["sentText"] == 1
    assert result["skippedUnsubscribed"] == 1
    assert result["failed"] == 0


def test_reminders_counts_failure_when_contact_detail_missing(table):
    import boto3
    from backend.lambda_src import index

    boto3.client("ses", region_name="us-east-1").verify_email_identity(EmailAddress=index.FROM_EMAIL)

    # preferredContact=text but no phone on file
    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, preferredContact="text")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    result = json.loads(resp["body"])
    assert result["failed"] == 1
    assert result["sentText"] == 0
