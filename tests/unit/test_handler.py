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
    assert updated["description"] == "Large"
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


def test_get_settings_defaults_to_empty_ready_date(table):
    from backend.lambda_src import index

    resp = index.handler(_event("GET", "/settings"), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"nextReadyDate": ""}


def test_update_settings_requires_admin(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01"}, claims=CUSTOMER_CLAIMS), None
    )
    assert resp["statusCode"] == 403


def test_admin_can_update_settings(table):
    from backend.lambda_src import index

    resp = index.handler(
        _event("PUT", "/settings", {"nextReadyDate": "2026-12-01"}, claims=ADMIN_CLAIMS), None
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"nextReadyDate": "2026-12-01"}

    get_resp = index.handler(_event("GET", "/settings"), None)
    assert json.loads(get_resp["body"]) == {"nextReadyDate": "2026-12-01"}


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


def test_create_order_stores_item_notes(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event(
            "POST", "/orders",
            {"items": [{"mealId": meal["mealId"], "quantity": 1, "note": "  hold the cheese  "}]},
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
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    order = json.loads(resp["body"])
    assert order["items"][0]["note"] == ""


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


def test_create_order_stores_the_current_ready_date(table):
    from backend.lambda_src import index

    index.handler(_event("PUT", "/settings", {"nextReadyDate": "2026-12-25"}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201
    assert json.loads(resp["body"])["readyDate"] == "2026-12-25"


def test_create_order_allowed_when_no_ready_date_is_set(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
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
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201


def test_create_order_rejected_inside_the_cutoff(table):
    from backend.lambda_src import index

    ready_date = (date.today() + timedelta(days=1)).isoformat()
    index.handler(_event("PUT", "/settings", {"nextReadyDate": ready_date}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_create_order_rejected_after_the_ready_date_has_passed(table):
    from backend.lambda_src import index

    ready_date = (date.today() - timedelta(days=1)).isoformat()
    index.handler(_event("PUT", "/settings", {"nextReadyDate": ready_date}, claims=ADMIN_CLAIMS), None)
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 400


def test_admin_cannot_place_an_order(table):
    from backend.lambda_src import index

    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims=ADMIN_CLAIMS),
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


def _place_order(index, claims, quantity=1):
    meal = _create_meal(index)
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": quantity}]}, claims=claims),
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
    other_claims = {"sub": "user-2", "email": "other@example.com"}
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
    other_claims = {"sub": "user-2", "email": "other@example.com"}
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


def test_create_order_emails_admins(table, monkeypatch):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")

    sent = {}
    monkeypatch.setattr(index.ses, "send_email", lambda **kwargs: sent.update(kwargs) or {"MessageId": "test"})

    _place_order(index, CUSTOMER_CLAIMS)

    assert sent["Destination"]["ToAddresses"] == ["admin@example.com"]
    # Sent from the admin's own address, not a hardcoded setting.
    assert sent["Source"] == "admin@example.com"
    assert "New order" in sent["Message"]["Subject"]["Data"]


def test_create_order_does_not_fail_if_admin_notification_errors(table):
    from backend.lambda_src import index

    # No Cognito pool/group set up for this test - USER_POOL_ID is the
    # module-level placeholder, so the admin lookup will fail. The order
    # must still succeed.
    resp = index.handler(
        _event("POST", "/orders", {"items": [{"mealId": _create_meal(index)["mealId"], "quantity": 1}]},
               claims=CUSTOMER_CLAIMS),
        None,
    )
    assert resp["statusCode"] == 201


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

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    # moto's SES mock enforces the same verified-sender rule real SES does.
    boto3.client("ses", region_name="us-east-1").verify_email_identity(EmailAddress="admin@example.com")

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

    cognito, pool_id = _create_test_pool(index)
    _create_admin_cognito_user(cognito, pool_id, "admin@example.com")
    boto3.client("ses", region_name="us-east-1").verify_email_identity(EmailAddress="admin@example.com")

    # preferredContact=text but no phone on file
    _save_profile(index, {"sub": "u1", "email": "u1@example.com"}, preferredContact="text")

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    result = json.loads(resp["body"])
    assert result["failed"] == 1
    assert result["sentText"] == 0


def test_reminders_fails_cleanly_when_no_admin_is_configured(table):
    from backend.lambda_src import index

    # A real pool with an Admins group that has no members - _from_email()
    # has nothing to resolve, and should fail the whole request up front
    # rather than fail every single recipient one at a time.
    _create_test_pool(index)

    resp = index.handler(_event("POST", "/reminders/send", claims=ADMIN_CLAIMS), None)
    assert resp["statusCode"] == 400


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


def test_delete_customer_does_not_delete_their_orders(table):
    from backend.lambda_src import index

    cognito, pool_id = _create_test_pool(index)
    sub = _create_cognito_user(cognito, pool_id, "jane@example.com")
    meal = _create_meal(index)
    index.handler(
        _event("POST", "/orders", {"items": [{"mealId": meal["mealId"], "quantity": 1}]}, claims={"sub": sub, "email": "jane@example.com"}),
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
