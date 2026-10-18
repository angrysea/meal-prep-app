"""Prep List page (admin-prep.html). window.print is stubbed by the
admin_page fixture to count calls instead of opening a print dialog."""
from playwright.sync_api import expect

from .helpers import assert_no_xss, expect_toast
from .mock_api import READY_DATE


def test_aggregates_placed_orders_for_ready_date(admin_page):
    page = admin_page
    page.open("/admin-prep.html")

    rows = page.locator("#prep-body tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Chicken Teriyaki Bowl")
    expect(rows.nth(0).locator("td.num")).to_have_text("2")
    assert_no_xss(page)


def test_enter_on_date_picker_prints_labels(admin_page):
    page = admin_page
    page.open("/admin-prep.html")
    page.press("#ready-date-filter", "Enter")

    expect_toast(page, f"Sent 3 labels (1 sheet) for {READY_DATE} to print")
    assert page.evaluate("window.__printed") == 1


def test_print_button_prints_one_label_per_meal(admin_page):
    page = admin_page
    page.open("/admin-prep.html")
    page.click("#print-labels-button")

    expect_toast(page, f"Sent 3 labels (1 sheet) for {READY_DATE} to print")
    labels = page.locator("#print-labels .label")
    expect(labels).to_have_count(3)
    # Customer-entered name and note land on the label as text, not markup.
    expect(labels.first).to_contain_text("Note: hold the sauce <img")
    assert_no_xss(page)


def test_print_labels_is_the_default_button(admin_page):
    page = admin_page
    page.open("/admin-prep.html")

    button = page.locator("#print-labels-button")
    expect(button).to_have_attribute("type", "submit")
    expect(button).not_to_have_class("secondary")
    expect(button).to_be_enabled()


def test_enter_prints_with_nothing_focused(admin_page):
    # Right after the page loads, focus is on the page itself, not the
    # date picker - Enter should still print.
    page = admin_page
    page.open("/admin-prep.html")
    assert page.evaluate("document.activeElement === document.body")
    page.keyboard.press("Enter")

    expect_toast(page, f"Sent 3 labels (1 sheet) for {READY_DATE} to print")
    assert page.evaluate("window.__printed") == 1


def test_enter_on_a_nav_link_still_follows_the_link(admin_page):
    page = admin_page
    page.open("/admin-prep.html")
    page.focus('header#nav a[href="/customers.html"]')
    page.keyboard.press("Enter")

    page.wait_for_url("**/customers.html")
    assert page.evaluate("window.__printed || 0") == 0


def test_enter_does_nothing_when_there_is_nothing_to_print(admin_page, mock_api):
    for order in mock_api.state["orders"]:
        order["status"] = "prepared"
    page = admin_page
    page.open("/admin-prep.html")
    expect(page.locator("#print-labels-button")).to_be_disabled()
    page.keyboard.press("Enter")

    page.wait_for_timeout(300)
    assert page.evaluate("window.__printed || 0") == 0


def test_label_names_the_pickup_person(admin_page, mock_api):
    mock_api.state["orders"][1]["pickupName"] = "Sam Lee"
    page = admin_page
    page.open("/admin-prep.html")
    page.click("#print-labels-button")

    expect(page.locator("#print-labels .label-customer", has_text="Bob Smith")).to_have_text(
        "Bob Smith (pickup: Sam Lee)"
    )
