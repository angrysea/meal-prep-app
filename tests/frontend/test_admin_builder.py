"""Builder page (admin.html): menu settings, meals, and add-ons."""
from playwright.sync_api import expect

from .helpers import expect_toast

MEAL_ROW = '#meals-body tr[data-meal-id="{}"]'
ADDON_ROW = '#addons-body tr[data-addon-id="{}"]'


def test_enter_in_any_field_adds_meal(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-name", "Salmon Plate")
    page.fill("#new-price", "14.50")
    page.fill("#new-fat", "20")
    page.press("#new-fat", "Enter")

    expect_toast(page, 'Added "Salmon Plate" to the menu')
    page.wait_for_load_state("networkidle")
    # One Enter, one create - the page's handler and the browser's own
    # implicit submission must not both fire.
    assert len(mock_api.writes()) == 1
    call = mock_api.writes()[-1]
    assert (call["method"], call["path"]) == ("POST", "/meals")
    assert call["body"]["name"] == "Salmon Plate"
    assert call["body"]["priceCents"] == 1450
    assert call["body"]["macros"] == {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 20}
    expect(page.locator("#new-name")).to_have_value("")
    expect(page.locator("#meals-body tr")).to_have_count(3)


def test_enter_on_checkbox_submits_its_form(admin_page, mock_api):
    # Browsers don't submit a form on Enter from a checkbox natively.
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-name", "Salmon Plate")
    page.fill("#new-price", "14.50")
    page.check("#new-staple")
    page.press("#new-staple", "Enter")

    expect_toast(page, 'Added "Salmon Plate" to the menu')
    assert mock_api.writes()[-1]["body"]["staple"] is True


def test_rapid_double_enter_creates_one_meal(admin_page, mock_api):
    # The default button is disabled while a save is in flight. Slow the
    # response so the second Enter lands before the first one finishes.
    mock_api.delay("POST", "/meals", 1.0)
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-name", "Salmon Plate")
    page.fill("#new-price", "14.50")
    page.press("#new-price", "Enter")
    page.press("#new-price", "Enter")

    expect_toast(page, 'Added "Salmon Plate" to the menu')
    page.wait_for_load_state("networkidle")
    assert [c["path"] for c in mock_api.writes()] == ["/meals"]


def test_required_fields_block_submit(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-price", "14.50")
    page.press("#new-price", "Enter")

    page.wait_for_timeout(300)
    assert mock_api.writes() == []


def test_enter_saves_next_ready_date(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#next-ready-date", "2026-11-02")
    page.press("#next-ready-date", "Enter")

    expect_toast(page, "Next ready date set to 2026-11-02")
    assert mock_api.writes()[-1]["body"] == {"nextReadyDate": "2026-11-02"}


def test_enter_on_sms_checkbox_saves_setting(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.check("#sms-enabled")
    page.press("#sms-enabled", "Enter")

    expect_toast(page, "SMS reminders turned on")
    assert mock_api.writes()[-1]["body"] == {"smsEnabled": True}


def test_edit_meal_dialog_saves_on_enter(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m1')} .edit-meal")
    expect(page.locator("#meal-edit-dialog")).to_be_visible()
    page.fill("#edit-meal-name", "Teriyaki Bowl")
    page.press("#edit-meal-name", "Enter")

    expect_toast(page, 'Saved changes to "Teriyaki Bowl"')
    expect(page.locator("#meal-edit-dialog")).to_be_hidden()
    call = mock_api.writes()[-1]
    assert (call["method"], call["path"]) == ("PUT", "/meals/m1")
    assert call["body"]["name"] == "Teriyaki Bowl"
    expect(page.locator(MEAL_ROW.format("m1"))).to_contain_text("Teriyaki Bowl")


def test_edit_meal_cancel_saves_nothing(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m1')} .edit-meal")
    page.fill("#edit-meal-name", "Something else")
    page.click("#meal-edit-cancel-button")

    expect(page.locator("#meal-edit-dialog")).to_be_hidden()
    assert mock_api.writes() == []


def test_hide_meal_shows_toast_and_updates_row(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m1')} .toggle-available")

    expect_toast(page, '"Chicken Teriyaki Bowl" is now hidden from the menu')
    assert mock_api.writes()[-1]["body"] == {"available": False}
    expect(page.locator(f"{MEAL_ROW.format('m1')} .toggle-available")).to_have_text("Show")


def test_failed_row_action_shows_error_toast(admin_page, mock_api):
    mock_api.fail("PUT", "/meals/m1", message="Meal is locked")
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m1')} .toggle-available")

    expect_toast(page, "Meal is locked", error=True)
    expect(page.locator(f"{MEAL_ROW.format('m1')} .toggle-available")).to_be_enabled()


def test_failed_create_shows_error_in_form_and_reenables_button(admin_page, mock_api):
    mock_api.fail("POST", "/meals", message="price must be positive")
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-name", "Salmon Plate")
    page.fill("#new-price", "14.50")
    page.press("#new-name", "Enter")

    form = page.locator("#meal-create-form")
    expect(form.locator(".error")).to_have_text("price must be positive")
    expect(form.locator('button[type="submit"]')).to_be_enabled()
    expect(page.locator("#new-name")).to_have_value("Salmon Plate")


def test_delete_meal_confirm_defaults_to_delete_on_enter(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m2')} .delete-meal")

    expect(page.locator("#confirm-dialog-message")).to_have_text('Delete "Turkey Chili"?')
    expect(page.locator("#confirm-dialog-ok-button")).to_be_focused()
    page.keyboard.press("Enter")

    expect_toast(page, 'Deleted "Turkey Chili"')
    assert (mock_api.writes()[-1]["method"], mock_api.writes()[-1]["path"]) == ("DELETE", "/meals/m2")
    expect(page.locator(MEAL_ROW.format("m2"))).to_have_count(0)


def test_escape_cancels_delete(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m2')} .delete-meal")
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.keyboard.press("Escape")

    expect(page.locator("#confirm-dialog")).to_be_hidden()
    assert mock_api.writes() == []
    expect(page.locator(MEAL_ROW.format("m2"))).to_have_count(1)


def test_cancel_button_cancels_delete(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{ADDON_ROW.format('a1')} .delete-addon")
    page.click('#confirm-dialog button[value="cancel"]')

    expect(page.locator("#confirm-dialog")).to_be_hidden()
    assert mock_api.writes() == []


def test_enter_adds_addon(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#addon-description", "Extra Protein")
    page.fill("#addon-price", "4")
    page.press("#addon-price", "Enter")

    expect_toast(page, 'Added the "Extra Protein" add-on')
    assert mock_api.writes()[-1]["body"]["priceCents"] == 400
    expect(page.locator("#addons-body tr")).to_have_count(2)


def test_edit_addon_dialog_saves_on_enter(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{ADDON_ROW.format('a1')} .edit-addon")
    page.fill("#edit-addon-price", "3.50")
    page.press("#edit-addon-price", "Enter")

    expect_toast(page, 'Saved changes to "Large"')
    assert mock_api.writes()[-1]["body"]["priceCents"] == 350


def test_hide_addon_shows_toast(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{ADDON_ROW.format('a1')} .toggle-addon-available")

    expect_toast(page, '"Large" is now hidden')


# ---------- Availability: Staple and a ready date are mutually exclusive ----------


def test_checking_staple_locks_ready_date(admin_page):
    page = admin_page
    page.open("/admin.html")
    page.check("#new-staple")

    expect(page.locator("#new-week-of")).to_be_disabled()
    expect(page.locator("#new-availability-hint")).to_have_text("Uncheck Staple to set a ready date instead.")

    page.uncheck("#new-staple")
    expect(page.locator("#new-week-of")).to_be_enabled()
    expect(page.locator("#new-availability-hint")).to_be_hidden()


def test_setting_ready_date_locks_staple_until_cleared(admin_page):
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-week-of", "2026-11-02")

    expect(page.locator("#new-staple")).to_be_disabled()
    expect(page.locator("#new-availability-hint")).to_have_text("Clear the ready date to make this a staple.")

    page.click("#new-week-of-clear")
    expect(page.locator("#new-week-of")).to_have_value("")
    expect(page.locator("#new-staple")).to_be_enabled()
    expect(page.locator("#new-week-of-clear")).to_be_disabled()


def test_add_form_unlocks_both_after_adding(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#new-name", "Salmon Plate")
    page.fill("#new-price", "14.50")
    page.check("#new-staple")
    page.press("#new-name", "Enter")

    expect_toast(page, 'Added "Salmon Plate" to the menu')
    body = mock_api.writes()[-1]["body"]
    assert (body["staple"], body["weekOf"]) == (True, "")
    expect(page.locator("#new-staple")).to_be_enabled()
    expect(page.locator("#new-week-of")).to_be_enabled()


def test_edit_dialog_locks_the_other_field(admin_page):
    page = admin_page
    page.open("/admin.html")

    # m1 is a staple: its ready date is locked.
    page.click(f"{MEAL_ROW.format('m1')} .edit-meal")
    expect(page.locator("#edit-meal-week-of")).to_be_disabled()
    expect(page.locator("#edit-meal-staple")).to_be_enabled()
    page.click("#meal-edit-cancel-button")

    # m2 has a ready date: Staple is locked.
    page.click(f"{MEAL_ROW.format('m2')} .edit-meal")
    expect(page.locator("#edit-meal-staple")).to_be_disabled()
    expect(page.locator("#edit-meal-week-of")).to_be_enabled()


def test_edit_dialog_switches_dated_meal_to_staple(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m2')} .edit-meal")
    page.click("#edit-meal-week-of-clear")
    page.check("#edit-meal-staple")
    page.press("#edit-meal-name", "Enter")

    expect_toast(page, 'Saved changes to "Turkey Chili"')
    body = mock_api.writes()[-1]["body"]
    assert (body["staple"], body["weekOf"]) == (True, "")


def test_meal_saved_with_both_can_still_be_fixed(admin_page, mock_api):
    # Saved before this rule existed: both fields lock, but Clear stays
    # usable so the dialog isn't stuck.
    mock_api.state["meals"][0]["weekOf"] = "2026-11-02"
    page = admin_page
    page.open("/admin.html")
    page.click(f"{MEAL_ROW.format('m1')} .edit-meal")

    expect(page.locator("#edit-meal-staple")).to_be_disabled()
    expect(page.locator("#edit-meal-week-of")).to_be_disabled()
    expect(page.locator("#edit-meal-week-of-clear")).to_be_enabled()

    page.click("#edit-meal-week-of-clear")
    expect(page.locator("#edit-meal-staple")).to_be_enabled()
    expect(page.locator("#edit-meal-staple")).to_be_checked()


# ---------- Toast position ----------


def _toast_center_offset(page):
    box = page.locator("#toast").bounding_box()
    return box["x"] + box["width"] / 2 - page.viewport_size["width"] / 2


def test_toast_is_centered_along_the_bottom(admin_page):
    page = admin_page
    page.open("/admin.html")
    page.check("#sms-enabled")
    page.press("#sms-enabled", "Enter")

    expect_toast(page, "SMS reminders turned on")
    page.wait_for_timeout(300)  # let the slide-in transition finish
    assert abs(_toast_center_offset(page)) <= 2
    box = page.locator("#toast").bounding_box()
    assert page.viewport_size["height"] - (box["y"] + box["height"]) < 40


def test_other_admin_pages_keep_toast_bottom_right(admin_page):
    page = admin_page
    page.open("/admin-orders.html")
    page.select_option('#orders-body tr[data-order-id="o2"] .status-select', "prepared")

    expect_toast(page, "Order for Bob Smith marked prepared")
    page.wait_for_timeout(300)
    box = page.locator("#toast").bounding_box()
    assert page.viewport_size["width"] - (box["x"] + box["width"]) < 40
