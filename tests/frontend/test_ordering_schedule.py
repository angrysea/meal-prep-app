"""Ordering open/closed: the closed card on the menu page, and the admin's
override on Manage menu. Whether ordering is open comes from the API
(GET /settings -> ordering); the schedule itself is unit-tested."""
from playwright.sync_api import expect

from .helpers import expect_toast


def _closed(mock_api):
    mock_api.state["schedule_open"] = False


def test_menu_is_shown_while_ordering_is_open(customer_page):
    page = customer_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_hidden()
    expect(page.locator(".meal-card")).to_have_count(1)


def test_closed_card_replaces_the_menu(customer_page, mock_api):
    _closed(mock_api)
    page = customer_page
    page.open("/index.html")

    card = page.locator("#ordering-closed")
    expect(card).to_be_visible()
    expect(card.locator("h2")).to_have_text("Ordering Is Currently Closed")
    expect(card).to_contain_text("Orders for this coming Monday's pickup are now closed.")
    expect(card).to_contain_text("Our next menu opens for ordering Monday at 11:00 AM, with pickup the following Monday.")
    expect(card).to_contain_text("Want to see what's coming up? Take a look at our monthly menu!")
    expect(page.locator("#meals")).to_be_hidden()
    expect(page.locator(".meal-card")).to_have_count(0)
    expect(page.locator("#ready-date-info")).to_be_hidden()
    expect(page.locator("#menu-intro")).to_be_hidden()
    # How It Works still shows above it.
    expect(page.locator("#how-it-works")).to_be_visible()


def test_view_monthly_menu_button(customer_page, mock_api):
    _closed(mock_api)
    page = customer_page
    page.open("/index.html")
    page.get_by_role("link", name="View Monthly Menu").click()

    page.wait_for_url("**/menu-flyer.html?period=monthly")


def test_logged_out_visitors_see_the_closed_card_too(anonymous_page, mock_api):
    _closed(mock_api)
    page = anonymous_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_visible()
    expect(page.locator("#login-prompt")).to_be_hidden()


def test_forced_closed_shows_the_closed_card(customer_page, mock_api):
    mock_api.state["settings"]["orderingMode"] = "closed"
    page = customer_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_visible()


def test_forced_open_shows_the_menu_on_the_weekend(customer_page, mock_api):
    _closed(mock_api)
    mock_api.state["settings"]["orderingMode"] = "open"
    page = customer_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_hidden()
    expect(page.locator(".add-to-order").first).to_be_enabled()


def test_edit_cart_is_not_reloaded_while_closed(customer_page, mock_api):
    _closed(mock_api)
    page = customer_page
    page.open("/orders.html")
    page.evaluate("""sessionStorage.setItem('editOrderCart', JSON.stringify(
        {items: [{mealId: 'm1', quantity: 1, selectedAddOnIds: []}], delivery: {}}))""")
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_visible()
    expect(page.locator("#cart-section")).to_be_hidden()
    expect(page.locator("#checkout-section")).to_be_hidden()


def test_admin_still_sees_the_menu_with_a_note(admin_page, mock_api):
    _closed(mock_api)
    page = admin_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_hidden()
    expect(page.locator(".meal-card")).to_have_count(1)
    expect(page.locator("#login-prompt")).to_contain_text("customers see the closed message")


# ---------- Manage menu: the override ----------


def test_ordering_card_shows_mode_and_status(admin_page):
    page = admin_page
    page.open("/admin.html")

    expect(page.locator("#ordering-mode")).to_have_value("auto")
    expect(page.locator("#ordering-status")).to_have_text("Customers can order right now.")


def test_forcing_ordering_closed(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.select_option("#ordering-mode", "closed")
    page.press("#ordering-mode", "Enter")

    expect_toast(page, "Ordering forced closed")
    assert mock_api.writes()[-1]["body"] == {"orderingMode": "closed"}
    status = page.locator("#ordering-status")
    expect(status).to_contain_text("Ordering is closed right now (forced).")
    expect(status).to_have_class("ordering-status is-forced")


def test_forcing_ordering_open_on_the_weekend(admin_page, mock_api):
    _closed(mock_api)
    page = admin_page
    page.open("/admin.html")
    expect(page.locator("#ordering-status")).to_have_text("Ordering is closed right now.")

    page.select_option("#ordering-mode", "open")
    page.click("#ordering-form button[type=submit]")

    expect_toast(page, "Ordering forced open")
    expect(page.locator("#ordering-status")).to_contain_text("Customers can order right now (forced).")


def test_switching_back_to_automatic(admin_page, mock_api):
    mock_api.state["settings"]["orderingMode"] = "closed"
    page = admin_page
    page.open("/admin.html")
    expect(page.locator("#ordering-mode")).to_have_value("closed")

    page.select_option("#ordering-mode", "auto")
    page.press("#ordering-mode", "Enter")

    expect_toast(page, "Ordering is back on the automatic schedule")
    expect(page.locator("#ordering-status")).to_have_text("Customers can order right now.")
