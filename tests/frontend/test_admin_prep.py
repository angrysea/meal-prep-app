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
