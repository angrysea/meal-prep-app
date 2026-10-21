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


# ---------- Labels: the add-on version's macros ----------


def _labels(page):
    page.open("/admin-prep.html")
    page.click("#print-labels-button")
    return page.locator("#print-labels .label")


def test_label_with_an_addon_shows_its_macros_and_description(admin_page):
    # Jane's 2 bowls are Large; the meal has Large macros (750 cal).
    labels = _labels(admin_page)
    jane = labels.filter(has_text="Jane").first
    expect(jane.locator(".label-macros")).to_contain_text("Calories: 750")
    expect(jane.locator(".label-macros")).to_contain_text("Protein: 60g")
    expect(jane.locator(".label-addon-line")).to_have_text("Large: 2 extra oz of chicken and 1 oz of broccoli.")


def test_label_without_an_addon_shows_the_meals_macros(admin_page, mock_api):
    mock_api.state["orders"][0]["items"][0]["selectedAddOns"] = []
    labels = _labels(admin_page)
    jane = labels.filter(has_text="Jane").first
    expect(jane.locator(".label-macros")).to_contain_text("Calories: 550")
    expect(jane.locator(".label-addon-line")).to_have_count(0)


def test_label_falls_back_to_the_orders_saved_macros_and_default_description(admin_page, mock_api):
    # The meal's add-on details were cleared after the order was placed.
    del mock_api.state["meals"][0]["addOnDetails"]
    labels = _labels(admin_page)
    jane = labels.filter(has_text="Jane").first
    expect(jane.locator(".label-macros")).to_contain_text("Calories: 740")
    expect(jane.locator(".label-addon-line")).to_have_text(
        "Large: Includes 2 extra ounces of protein and 1 extra ounce of vegetables."
    )


def test_addon_with_no_macros_anywhere_shows_no_macros(admin_page, mock_api):
    # Rather than the regular meal's numbers, as if they applied.
    del mock_api.state["meals"][0]["addOnDetails"]
    del mock_api.state["orders"][0]["items"][0]["selectedAddOns"][0]["macros"]
    labels = _labels(admin_page)
    expect(labels.filter(has_text="Jane").first.locator(".label-macros")).to_have_count(0)


def test_prep_table_shows_macros_for_the_meal_or_its_addon(admin_page):
    page = admin_page
    page.open("/admin-prep.html")

    headers = [h.strip() for h in page.locator("#prep-table th").all_text_contents()]
    assert [h.title() for h in headers] == ["Meal", "Add-On", "Note", "Macros", "Quantity"]
    bowl = page.locator("#prep-body tr").filter(has_text="Chicken Teriyaki Bowl")
    expect(bowl.locator("td").nth(1)).to_contain_text("Large")
    expect(bowl.locator(".cell-sub")).to_have_text("2 extra oz of chicken and 1 oz of broccoli.")
    expect(bowl.locator(".prep-macros")).to_have_text("750 cal · 60g protein · 70g carbs · 18g fat")
    chili = page.locator("#prep-body tr").filter(has_text="Turkey Chili")
    expect(chili.locator(".prep-macros")).to_have_text("480 cal · 38g protein · 40g carbs · 14g fat")


def test_labels_are_only_for_the_selected_ready_date(admin_page, mock_api):
    # An order for a later week must not print with this week's labels.
    later = dict(mock_api.state["orders"][0], orderId="later", readyDate="2099-01-05",
                 customerName="Later Customer", pickupName="")
    mock_api.state["orders"].append(later)
    page = admin_page
    page.open("/admin-prep.html")

    page.click("#print-labels-button")
    expect_toast(page, f"Sent 3 labels (1 sheet) for {READY_DATE} to print")
    expect(page.locator("#print-labels .label", has_text="Later Customer")).to_have_count(0)

    page.select_option("#ready-date-filter", "2099-01-05")
    page.click("#print-labels-button")
    expect_toast(page, "Sent 2 labels (1 sheet) for 2099-01-05 to print")
    expect(page.locator("#print-labels .label")).to_have_count(2)
    expect(page.locator("#print-labels .label", has_text="Later Customer")).to_have_count(2)


def test_browser_print_after_changing_the_date_prints_that_dates_labels(admin_page, mock_api):
    # Printing with Cmd+P / File > Print (not the button) must match the picker too.
    later = dict(mock_api.state["orders"][0], orderId="later", readyDate="2099-01-05",
                 customerName="Later Customer", pickupName="")
    mock_api.state["orders"].append(later)
    page = admin_page
    page.open("/admin-prep.html")
    expect(page.locator("#print-labels .label")).to_have_count(3)  # ready before any click

    page.click("#print-labels-button")
    page.select_option("#ready-date-filter", "2099-01-05")

    labels = page.locator("#print-labels .label")
    expect(labels).to_have_count(2)
    expect(labels.filter(has_text="Later Customer")).to_have_count(2)
