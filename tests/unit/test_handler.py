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


SIZE_GROUP = {
    "id": "size",
    "name": "Size",
    "selectionType": "single",
    "required": True,
    "options": [
        {"id": "regular", "label": "Regular", "priceDeltaCents": 0},
        {"id": "large", "label": "Large", "priceDeltaCents": 300},
    ],
}
ADDONS_GROUP = {
    "id": "addons",
    "name": "Add-ons",
    "selectionType": "multi",
    "required": False,
    "options": [
        {"id": "extra_protein", "label": "Extra Protein", "priceDeltaCents": 150},
        {"id": "extra_rice", "label": "Extra Rice", "priceDeltaCents": 100},
    ],
}


def test_create_meal_with_macros_and_option_groups(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event(
            "POST", "/meals",
            {
                "name": "Chicken Teriyaki Bowl",
                "priceCents": 1200,
                "macros": {"calories": 540, "proteinG": 48, "carbsG": 52, "fatG": 14},
                "optionGroups": [SIZE_GROUP, ADDONS_GROUP],
            },
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    meal = json.loads(resp["body"])
    assert meal["macros"] == {"calories": 540, "proteinG": 48, "carbsG": 52, "fatG": 14}
    assert [g["id"] for g in meal["optionGroups"]] == ["size", "addons"]


def test_create_meal_defaults_macros_and_option_groups_when_omitted(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    assert meal["macros"] == {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0}
    assert meal["optionGroups"] == []


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


def _create_meal_with_options(index, price_cents=1200):
    resp = index.handler(
        _event(
            "POST", "/meals",
            {"name": "Chicken Teriyaki Bowl", "priceCents": price_cents, "optionGroups": [SIZE_GROUP, ADDONS_GROUP]},
            claims=ADMIN_CLAIMS,
        ),
        None,
    )
    return json.loads(resp["body"])


def test_create_order_adds_single_select_option_price(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index, price_cents=1200)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
                "mealId": meal["mealId"], "quantity": 1,
                "selectedOptions": [{"groupId": "size", "optionId": "large"}],
            }]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    assert order["totalCents"] == 1500  # 1200 base + 300 for Large
    assert order["items"][0]["selectedOptions"] == [
        {"groupId": "size", "groupName": "Size", "optionId": "large", "optionLabel": "Large", "priceDeltaCents": 300}
    ]


def test_create_order_sums_multi_select_option_prices(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index, price_cents=1200)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
                "mealId": meal["mealId"], "quantity": 2,
                "selectedOptions": [
                    {"groupId": "size", "optionId": "regular"},
                    {"groupId": "addons", "optionId": "extra_protein"},
                    {"groupId": "addons", "optionId": "extra_rice"},
                ],
            }]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 201
    order = json.loads(resp["body"])
    # unit price: 1200 + 0 (regular) + 150 + 100 = 1450, times quantity 2
    assert order["totalCents"] == 2900
    assert order["items"][0]["unitPriceCents"] == 1450


def test_create_order_fails_when_required_option_group_missing(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_fails_for_unknown_option_group(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
                "mealId": meal["mealId"], "quantity": 1,
                "selectedOptions": [{"groupId": "nonexistent", "optionId": "x"}],
            }]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_fails_for_unknown_option_in_known_group(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
                "mealId": meal["mealId"], "quantity": 1,
                "selectedOptions": [{"groupId": "size", "optionId": "extra-large"}],
            }]},
            claims=CUSTOMER_CLAIMS,
        ),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_fails_for_multiple_selections_in_single_select_group(table):
    from backend.lambda_src import index

    meal = _create_meal_with_options(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{
                "mealId": meal["mealId"], "quantity": 1,
                "selectedOptions": [
                    {"groupId": "size", "optionId": "regular"},
                    {"groupId": "size", "optionId": "large"},
                ],
            }]},
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
