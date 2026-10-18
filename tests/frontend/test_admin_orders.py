"""Orders page (admin-orders.html)."""
from playwright.sync_api import expect

from .helpers import assert_no_xss, expect_toast
from .mock_api import READY_DATE

ROW = '#orders-body tr[data-order-id="{}"]'


def test_lists_orders_in_a_table(admin_page):
    page = admin_page
    page.open("/admin-orders.html")

    expect(page.locator("#orders-body tr")).to_have_count(2)
    expect(page.locator("#orders-count")).to_have_text("2 orders")
    bob = page.locator(ROW.format("o2"))
    expect(bob).to_contain_text("Bob Smith")
    expect(bob).to_contain_text("1 x Turkey Chili")
    expect(bob).to_contain_text("$11.50")
    expect(bob).to_contain_text("(555) 555-9876")


def test_order_table_has_no_email_or_address(admin_page):
    page = admin_page
    page.open("/admin-orders.html")

    headers = page.locator("#orders-table th").all_inner_texts()
    assert [h.title() for h in headers] == ["Placed", "Customer", "Phone", "Ready Date", "Items", "Total", "Status"]
    expect(page.locator("#orders-body")).not_to_contain_text("@example.com")


def test_customer_entered_text_is_escaped(admin_page):
    page = admin_page
    page.open("/admin-orders.html")

    jane = page.locator(ROW.format("o1"))
    expect(jane).to_contain_text('Jane <img src=x onerror="window.__xss=1">')
    expect(jane).to_contain_text("hold the sauce <img")
    assert_no_xss(page)


def test_status_change_saves_and_shows_toast(admin_page, mock_api):
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option(f"{ROW.format('o2')} .status-select", "prepared")

    expect_toast(page, "Order for Bob Smith marked prepared")
    call = mock_api.writes()[-1]
    assert (call["method"], call["path"], call["body"]) == ("PUT", "/admin/orders/u2/o2", {"status": "prepared"})
    # The default filter is "placed", so the now-prepared order drops off.
    expect(page.locator(ROW.format("o2"))).to_have_count(0)


def test_failed_status_change_reverts_dropdown(admin_page, mock_api):
    mock_api.fail("PUT", "/admin/orders/u2/o2", message="Order already picked up")
    page = admin_page
    page.open("/admin-orders.html")
    select = page.locator(f"{ROW.format('o2')} .status-select")
    select.select_option("delivered")

    expect_toast(page, "Order already picked up", error=True)
    expect(select).to_have_value("placed")
    expect(select).to_be_enabled()


def test_filter_with_no_matches_shows_empty_message(admin_page):
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option("#status-filter", "cancelled")

    expect(page.locator("#orders-body")).to_have_text("No orders in cancelled status right now.")
    expect(page.locator("#orders-count")).to_have_text("0 orders")


# ---------- Ready date filter ----------


def _with_second_ready_date(mock_api):
    # Bob's order is for a later ready date than Jane's.
    mock_api.state["orders"][1]["readyDate"] = "2099-01-05"


def test_ready_date_filter_sits_before_show(admin_page):
    page = admin_page
    page.open("/admin-orders.html")

    labels = page.locator(".toolbar label").all_inner_texts()
    assert labels[:2] == ["Ready date", "Show"]
    expect(page.locator("#ready-date-filter")).to_have_value("all")


def test_ready_date_options_include_order_dates_and_next_ready_date(admin_page, mock_api):
    _with_second_ready_date(mock_api)
    page = admin_page
    page.open("/admin-orders.html")

    options = page.locator("#ready-date-filter option").all_inner_texts()
    assert options == ["All dates", READY_DATE, "2099-01-05"]


def test_filtering_by_ready_date(admin_page, mock_api):
    _with_second_ready_date(mock_api)
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option("#ready-date-filter", "2099-01-05")

    expect(page.locator("#orders-body tr")).to_have_count(1)
    expect(page.locator(ROW.format("o2"))).to_be_visible()
    expect(page.locator("#orders-count")).to_have_text("1 order")

    page.select_option("#ready-date-filter", "all")
    expect(page.locator("#orders-body tr")).to_have_count(2)


def test_ready_date_and_show_filters_combine(admin_page, mock_api):
    _with_second_ready_date(mock_api)
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option("#ready-date-filter", "2099-01-05")
    page.select_option("#status-filter", "cancelled")

    # The selected date stays put even though no cancelled order has it.
    expect(page.locator("#ready-date-filter")).to_have_value("2099-01-05")
    expect(page.locator("#orders-body")).to_have_text("No orders in cancelled status for 2099-01-05.")
    expect(page.locator("#orders-count")).to_have_text("0 orders")


def test_status_change_keeps_the_ready_date_filter(admin_page, mock_api):
    _with_second_ready_date(mock_api)
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option("#ready-date-filter", "2099-01-05")
    page.select_option(f"{ROW.format('o2')} .status-select", "prepared")

    expect_toast(page, "Order for Bob Smith marked prepared")
    expect(page.locator("#ready-date-filter")).to_have_value("2099-01-05")
    expect(page.locator("#orders-body")).to_have_text("No orders in placed status for 2099-01-05.")


def test_shows_pickup_person_when_someone_else_collects(admin_page, mock_api):
    mock_api.state["orders"][1]["pickupName"] = "Sam Lee"
    page = admin_page
    page.open("/admin-orders.html")

    expect(page.locator(f"{ROW.format('o2')} .pickup-by")).to_have_text("Pickup: Sam Lee")
    expect(page.locator(f"{ROW.format('o1')} .pickup-by")).to_have_count(0)
