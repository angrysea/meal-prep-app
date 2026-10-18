"""Orders page (admin-orders.html)."""
from playwright.sync_api import expect

from .helpers import assert_no_xss, expect_toast

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
