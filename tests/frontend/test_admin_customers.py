"""Customers page (customers.html)."""
from playwright.sync_api import expect

from .helpers import assert_no_xss, expect_toast

ROW = '#customers-body tr[data-username="{}"]'


def test_lists_customers_with_formatted_phone(admin_page):
    page = admin_page
    page.open("/customers.html")

    expect(page.locator("#customers-body tr")).to_have_count(2)
    expect(page.locator("#customers-count")).to_have_text("2 customers")
    expect(page.locator(ROW.format("u1"))).to_contain_text("(555) 555-1234")


def test_customer_entered_text_is_escaped(admin_page):
    page = admin_page
    page.open("/customers.html")

    expect(page.locator(ROW.format("u1"))).to_contain_text('Jane <img src=x onerror="window.__xss=1">')
    assert_no_xss(page)


def test_edit_dialog_saves_on_enter_from_radio(admin_page, mock_api):
    page = admin_page
    page.open("/customers.html")
    page.click(f"{ROW.format('u2')} .edit-customer")
    page.check("#edit-contact-email")
    page.press("#edit-contact-email", "Enter")

    expect_toast(page, "Saved changes to bob@example.com")
    call = mock_api.writes()[-1]
    assert (call["method"], call["path"]) == ("PUT", "/customers/u2")
    assert call["body"]["preferredContact"] == "email"
    expect(page.locator("#edit-dialog")).to_be_hidden()


def test_failed_save_keeps_dialog_open_with_error(admin_page, mock_api):
    mock_api.fail("PUT", "/customers/u2", message="email already in use")
    page = admin_page
    page.open("/customers.html")
    page.click(f"{ROW.format('u2')} .edit-customer")
    page.press("#edit-name", "Enter")

    expect(page.locator("#edit-dialog .error")).to_have_text("email already in use")
    expect(page.locator("#edit-dialog")).to_be_visible()


def test_delete_confirms_then_shows_toast(admin_page, mock_api):
    page = admin_page
    page.open("/customers.html")
    page.click(f"{ROW.format('u2')} .delete-customer")

    expect(page.locator("#confirm-dialog-message")).to_have_text(
        "Delete bob@example.com's account? This cannot be undone."
    )
    page.keyboard.press("Enter")

    expect_toast(page, "Deleted bob@example.com's account")
    assert (mock_api.writes()[-1]["method"], mock_api.writes()[-1]["path"]) == ("DELETE", "/customers/u2")
    expect(page.locator(ROW.format("u2"))).to_have_count(0)


def test_send_reminders_confirms_and_reports_result(admin_page, mock_api):
    page = admin_page
    page.open("/customers.html")
    page.click("#send-reminders-button")

    expect(page.locator("#confirm-dialog-ok-button")).to_have_text("Send reminders")
    page.keyboard.press("Enter")

    summary = "Sent 1 email(s), 0 text(s). Skipped 1 unsubscribed. 0 failed."
    expect_toast(page, summary)
    expect(page.locator("#reminders-result")).to_have_text(summary)
    assert mock_api.writes()[-1]["path"] == "/reminders/send"


def test_cancelled_reminders_send_nothing(admin_page, mock_api):
    page = admin_page
    page.open("/customers.html")
    page.click("#send-reminders-button")
    page.keyboard.press("Escape")

    expect(page.locator("#confirm-dialog")).to_be_hidden()
    assert mock_api.writes() == []


def test_edit_dialog_says_address_not_delivery_address(admin_page):
    page = admin_page
    page.open("/customers.html")

    expect(page.locator('label[for="edit-address"]')).to_have_text("Address")


def test_admin_can_set_a_customers_pickup_location(admin_page, mock_api):
    mock_api.state["customers"][1]["pickupLocation"] = "Elgin"
    page = admin_page
    page.open("/customers.html")
    # Only in the edit dialog, not the table.
    expect(page.locator("#customers-table")).not_to_contain_text("Elgin")
    page.click(f"{ROW.format('u2')} .edit-customer")
    field = page.get_by_label("Pickup location")
    expect(field).to_have_value("Elgin")
    field.fill("  Elgin - Main St  ")
    page.press("#edit-pickup-location", "Enter")

    expect_toast(page, "Saved changes to bob@example.com")
    assert mock_api.writes()[-1]["body"]["pickupLocation"] == "Elgin - Main St"


def test_blank_pickup_location_shows_the_default_as_a_hint(admin_page):
    page = admin_page
    page.open("/customers.html")
    page.click(f"{ROW.format('u1')} .edit-customer")
    expect(page.locator("#edit-pickup-location")).to_have_value("")
    expect(page.locator("#edit-pickup-location")).to_have_attribute("placeholder", "The Gym TX, Georgetown")
