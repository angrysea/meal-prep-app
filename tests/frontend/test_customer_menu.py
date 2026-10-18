"""Customer menu page (index.html): the current order sits above the meal
grid so it's plainly visible; checkout details stay below the menu."""
import pytest
from playwright.sync_api import expect

ADD_M1 = '.add-to-order[data-meal-id="m1"]'


def _top(page, selector):
    return page.locator(selector).bounding_box()["y"]


def test_order_summary_hidden_until_something_is_ordered(customer_page):
    page = customer_page
    page.open("/index.html")

    expect(page.locator("#cart-section")).to_be_hidden()
    expect(page.locator("#checkout-section")).to_be_hidden()


def test_ordered_meals_appear_above_the_menu(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)

    summary = page.locator("#cart-section")
    expect(summary).to_be_visible()
    expect(summary.locator("h2")).to_have_text("Your order")
    expect(summary.locator(".cart-line")).to_contain_text("Chicken Teriyaki Bowl")
    expect(page.locator("#cart-total")).to_have_text("$12.99")
    assert _top(page, "#cart-section") < _top(page, "#meals")
    # Checkout details stay below the menu.
    assert _top(page, "#checkout-section") > _top(page, "#meals")


def test_quantity_changes_update_the_summary(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    page.click(ADD_M1)

    expect(page.locator("#cart-lines .qty-input")).to_have_value("2")
    expect(page.locator("#cart-total")).to_have_text("$25.98")

    page.click("#cart-lines .qty-minus")
    page.click("#cart-lines .qty-minus")
    expect(page.locator("#cart-section")).to_be_hidden()
    expect(page.locator("#checkout-section")).to_be_hidden()


def test_check_out_button_jumps_to_checkout(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    page.click("#checkout-jump")

    expect(page).to_have_url(page.url.split("#")[0] + "#checkout-section")
    expect(page.locator("#checkout-button")).to_be_in_viewport()


def test_cancel_clears_the_order(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    page.click("#cancel-order-button")

    expect(page.locator("#cart-section")).to_be_hidden()
    expect(page.locator("#checkout-section")).to_be_hidden()


# ---------- Checkout: name from profile, per-order phone, pickup person ----------


def _start_checkout(page):
    page.open("/index.html")
    page.click(ADD_M1)


def _placed_order(mock_api):
    return next(c for c in mock_api.writes() if c["path"] == "/orders")


def test_checkout_shows_profile_name_and_prefills_phone(customer_page):
    page = customer_page
    _start_checkout(page)

    expect(page.locator("#checkout-customer")).to_have_text("Ordering as Test Customer")
    expect(page.locator("#checkout-customer a")).to_have_count(0)
    expect(page.locator("#contact-phone")).to_have_value("(555) 222-3333")
    expect(page.locator("#pickup-name")).to_have_value("")


def test_checkout_has_no_editable_name_email_or_address(customer_page):
    page = customer_page
    _start_checkout(page)

    checkout = page.locator("#checkout-section")
    # The name is shown as text, not an input.
    expect(checkout.locator("#checkout-customer-name")).to_have_text("Test Customer")
    for field in ("delivery-name", "delivery-email", "delivery-address"):
        expect(page.locator(f"#{field}")).to_have_count(0)
    expect(checkout.locator("input")).to_have_count(2)  # contact phone + pickup name
    expect(checkout).not_to_contain_text("Address")


def test_place_order_sends_phone_and_pickup_but_not_name(customer_page, mock_api):
    page = customer_page
    _start_checkout(page)
    page.fill("#contact-phone", "5559998888")
    page.fill("#pickup-name", "  Sam Lee ")
    page.click("#checkout-button")

    page.wait_for_url("**/orders.html")
    assert _placed_order(mock_api)["body"] == {
        "items": [{"mealId": "m1", "quantity": 1, "selectedAddOnIds": [], "note": ""}],
        "contactPhone": "(555) 999-8888",
        "pickupName": "Sam Lee",
    }
    card = page.locator(".order-card")
    expect(card).to_contain_text("Test Customer - (555) 999-8888")
    expect(card).to_contain_text("Pickup: Sam Lee")


def test_blank_pickup_means_the_customer_picks_up(customer_page, mock_api):
    page = customer_page
    _start_checkout(page)
    page.click("#checkout-button")

    page.wait_for_url("**/orders.html")
    assert _placed_order(mock_api)["body"]["pickupName"] == ""
    expect(page.locator(".order-card")).to_contain_text("Pickup: you")


def test_placing_an_order_doesnt_change_the_profile(customer_page, mock_api):
    # The phone entered at checkout belongs to that order only.
    page = customer_page
    _start_checkout(page)
    page.fill("#contact-phone", "5559998888")
    page.click("#checkout-button")

    page.wait_for_url("**/orders.html")
    assert [c for c in mock_api.writes() if c["path"] == "/profile"] == []
    assert mock_api.state["profile"]["phone"] == "(555) 222-3333"


def test_contact_phone_is_optional(customer_page, mock_api):
    page = customer_page
    _start_checkout(page)
    expect(page.locator('label[for="contact-phone"]')).to_contain_text("(optional)")
    page.fill("#contact-phone", "")
    page.click("#checkout-button")

    page.wait_for_url("**/orders.html")
    assert _placed_order(mock_api)["body"]["contactPhone"] == ""


def test_partial_contact_phone_is_rejected(customer_page, mock_api):
    page = customer_page
    _start_checkout(page)
    page.fill("#contact-phone", "555123")
    page.click("#checkout-button")

    expect(page.locator("#checkout-error")).to_have_text(
        "Please enter a 10-digit contact phone number, or leave it blank."
    )
    assert [c for c in mock_api.writes() if c["path"] == "/orders"] == []


def test_contact_phone_is_masked_as_you_type(customer_page):
    page = customer_page
    _start_checkout(page)
    phone = page.locator("#contact-phone")
    phone.fill("")
    expect(phone).to_have_attribute("placeholder", "(___) ___-____")

    phone.press_sequentially("555")
    expect(phone).to_have_value("(555")
    phone.press_sequentially("1234")
    expect(phone).to_have_value("(555) 123-4")
    # Letters are dropped, and nothing past 10 digits is kept.
    phone.press_sequentially("x567899")
    expect(phone).to_have_value("(555) 123-4567")


def test_profile_without_a_name_blocks_ordering(customer_page, mock_api):
    mock_api.state["profile"]["name"] = ""
    page = customer_page
    _start_checkout(page)

    expect(page.locator("#checkout-no-name")).to_be_visible()
    expect(page.locator("#checkout-no-name a")).to_have_attribute("href", "/account.html")
    expect(page.locator("#checkout-customer")).to_be_hidden()
    expect(page.locator("#checkout-button")).to_be_disabled()


@pytest.mark.parametrize("path", ["/account.html", "/login.html"])
def test_customer_pages_say_address_not_delivery_address(customer_page, path):
    page = customer_page
    page.open(path)

    assert "Delivery address" not in page.content()


# ---------- Menu flyer links ----------


def test_menu_page_has_no_flyer_buttons_for_customers(customer_page):
    page = customer_page
    page.open("/index.html")

    expect(page.get_by_role("link", name="View this week's menu")).to_be_hidden()
    expect(page.get_by_role("link", name="View this month's menu")).to_be_hidden()


def test_monthly_menu_is_a_nav_item_after_my_orders(customer_page):
    page = customer_page
    page.open("/index.html")

    links = page.locator("#nav-links a").all_inner_texts()
    assert links[links.index("My Orders") + 1] == "Monthly Menu"
    page.click('#nav-links a:has-text("Monthly Menu")')
    page.wait_for_url("**/menu-flyer.html?period=monthly")


def test_admin_keeps_flyer_buttons_to_send_menus(admin_page):
    # The flyer pages are where the admin's "Send this menu to customers"
    # button lives - without these the admin couldn't reach it.
    page = admin_page
    page.open("/index.html")

    expect(page.get_by_role("link", name="View this week's menu")).to_be_visible()
    expect(page.get_by_role("link", name="View this month's menu")).to_be_visible()
    expect(page.locator("#nav-links a", has_text="Monthly Menu")).to_have_count(0)
