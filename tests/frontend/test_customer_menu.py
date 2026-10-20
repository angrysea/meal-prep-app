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


# ---------- How It Works ----------


def test_how_it_works_sits_above_this_weeks_menu(customer_page):
    page = customer_page
    page.open("/index.html")

    section = page.locator("#how-it-works")
    expect(section).to_be_visible()
    expect(section.locator("summary")).to_have_text("How It Works")
    expect(section).to_contain_text("Our weekly menu is updated every Monday by 11:00 AM.")
    expect(section).to_contain_text("Orders are not confirmed until payment is received.")
    assert _top(page, "#how-it-works") < _top(page, "h1")


def test_how_it_works_links(customer_page):
    page = customer_page
    page.open("/index.html")
    section = page.locator("#how-it-works")

    directions = page.locator("#pickup-directions")
    expect(directions).to_have_text("4112 Williams Drive #105, Georgetown TX 78628")
    href = directions.get_attribute("href")
    assert href.startswith("https://www.google.com/maps/dir/?api=1&destination=")
    assert "4112+Williams+Drive+%23105" in href
    expect(directions).to_have_attribute("target", "_blank")
    expect(page.locator("#venmo-link")).to_have_attribute("href", "https://venmo.com/u/lgraffeo88")
    expect(page.locator("#contact-email-link")).to_have_attribute("href", "mailto:gtxmeals@gmail.com")
    expect(section.locator("li").filter(has_text="Elgin")).to_contain_text(
        "Please email us to make arrangements before placing your order."
    )
    expect(section.locator("p").filter(has_text="Questions or Other Payment Options?")).to_have_text(
        "Questions or Other Payment Options? Email us at gtxmeals@gmail.com."
    )


def test_how_it_works_shows_for_logged_out_visitors(anonymous_page):
    page = anonymous_page
    page.open("/index.html")

    expect(page.locator("#how-it-works")).to_be_visible()


def test_collapsing_how_it_works_is_remembered(customer_page):
    page = customer_page
    page.open("/index.html")
    is_open = "document.getElementById('how-it-works').open"
    # The toggle event (which saves the choice) fires asynchronously.
    saved = "v => localStorage.getItem('mealprep.howItWorksCollapsed') === v"

    page.click("#how-it-works summary")
    expect(page.locator("#how-it-works p").first).to_be_hidden()
    page.wait_for_function(saved, arg="1")
    page.reload()
    page.wait_for_function(f"!{is_open}")

    page.click("#how-it-works summary")
    page.wait_for_function(saved, arg="0")
    page.reload()
    assert page.evaluate(is_open)


# ---------- Payment reminder ----------


def test_payment_reminder_sits_under_not_a_checkout(customer_page):
    page = customer_page
    _start_checkout(page)

    warning = page.locator("#payment-warning")
    expect(warning).to_have_text(
        "IMPORTANT: If payment is not received by Saturday morning and you have not made "
        "prior payment arrangements with us, your order will be canceled."
    )
    assert warning.evaluate("el => getComputedStyle(el).fontWeight") == "700"
    assert warning.evaluate("el => el.previousElementSibling.id") == "checkout-note"
    # Customers see it before they submit.
    assert _top(page, "#payment-warning") < _top(page, "#checkout-button") + 200
    expect(warning).to_be_visible()


# ---------- Add-ons: pick one; each meal has its own description and macros ----------

CARD = '.meal-card[data-meal-id="m1"]'


def _choose(page, label):
    page.locator(CARD).get_by_label(label, exact=True).check()


def test_addons_are_a_single_choice_starting_at_regular(customer_page):
    page = customer_page
    page.open("/index.html")

    radios = page.locator(f"{CARD} .addon-choice")
    expect(radios).to_have_count(3)
    assert all(r.get_attribute("type") == "radio" for r in radios.all())
    expect(page.locator(CARD).get_by_label("Regular", exact=True)).to_be_checked()
    expect(page.locator(CARD)).to_contain_text("Large (+$3.00)")
    expect(page.locator(CARD)).to_contain_text("Large with Cauliflower Rice (+$4.00)")

    _choose(page, "Large (+$3.00)")
    _choose(page, "Large with Cauliflower Rice (+$4.00)")
    expect(page.locator(f"{CARD} .addon-choice:checked")).to_have_count(1)


def _option(page, label):
    return page.locator(f"{CARD} .option-choice").filter(has_text=label).first


def test_each_addon_lists_its_description_and_macros(customer_page):
    page = customer_page
    page.open("/index.html")

    large = _option(page, "Large (+$3.00)")
    expect(large.locator(".addon-description")).to_have_text("2 extra oz of chicken and 1 oz of broccoli.")
    expect(large.locator(".addon-macros")).to_have_text("750 cal · 60g protein · 70g carbs · 18g fat")
    # Description first, then macros - both under the option's name.
    assert large.locator(".addon-description").bounding_box()["y"] < large.locator(".addon-macros").bounding_box()["y"]
    name_x = large.get_by_text("Large (+$3.00)").bounding_box()["x"]
    assert abs(large.locator(".addon-macros").bounding_box()["x"] - name_x) < 1
    # Shown without choosing anything.
    expect(page.locator(CARD).get_by_label("Regular", exact=True)).to_be_checked()


def test_regular_has_no_extra_lines(customer_page):
    page = customer_page
    page.open("/index.html")

    regular = _option(page, "Regular")
    expect(regular.locator(".addon-description, .addon-macros")).to_have_count(0)


def test_choosing_an_addon_switches_the_cards_macros_line(customer_page):
    page = customer_page
    page.open("/index.html")
    macros = page.locator(f"{CARD} .macros")
    expect(macros).to_have_text("550 cal · 42g protein · 60g carbs · 12g fat")

    _choose(page, "Large (+$3.00)")
    expect(macros).to_have_text("Large: 750 cal · 60g protein · 70g carbs · 18g fat")

    _choose(page, "Regular")
    expect(macros).to_have_text("550 cal · 42g protein · 60g carbs · 12g fat")


def test_addon_without_meal_details_uses_the_default_description(customer_page):
    page = customer_page
    page.open("/index.html")

    combo = _option(page, "Large with Cauliflower Rice (+$4.00)")
    expect(combo.locator(".addon-description")).to_have_text("Large, with cauliflower rice instead of rice.")
    # No macros entered for this meal yet - no macros line, rather than the
    # regular meal's numbers as if they applied.
    expect(combo.locator(".addon-macros")).to_have_count(0)
    _choose(page, "Large with Cauliflower Rice (+$4.00)")
    expect(page.locator(f"{CARD} .macros")).to_be_hidden()


def test_ordering_with_an_addon(customer_page, mock_api):
    page = customer_page
    page.open("/index.html")
    _choose(page, "Large (+$3.00)")
    page.click(ADD_M1)

    lines = page.locator("#cart-lines")
    expect(lines).to_contain_text("Large - 750 cal")
    expect(page.locator("#cart-total")).to_have_text("$15.99")
    # The card goes back to Regular for the next one.
    expect(page.locator(CARD).get_by_label("Regular", exact=True)).to_be_checked()
    expect(page.locator(f"{CARD} .macros")).to_have_text("550 cal · 42g protein · 60g carbs · 12g fat")

    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")
    assert _placed_order(mock_api)["body"]["items"][0]["selectedAddOnIds"] == ["a1"]
    expect(page.locator(".order-card")).to_contain_text("Large")


def test_regular_orders_send_no_addon(customer_page, mock_api):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    page.click("#checkout-button")

    page.wait_for_url("**/orders.html")
    assert _placed_order(mock_api)["body"]["items"][0]["selectedAddOnIds"] == []


def test_logged_out_visitors_can_compare_options(anonymous_page):
    page = anonymous_page
    page.open("/index.html")
    _choose(page, "Large (+$3.00)")

    expect(page.locator(f"{CARD} .macros")).to_have_text("Large: 750 cal · 60g protein · 70g carbs · 18g fat")
    expect(page.locator(ADD_M1)).to_be_disabled()


def test_hidden_addons_are_not_offered(customer_page, mock_api):
    mock_api.state["addOns"][1]["available"] = False
    page = customer_page
    page.open("/index.html")

    expect(page.locator(f"{CARD} .addon-choice")).to_have_count(2)  # Regular + Large


def test_addon_text_is_escaped_on_the_menu(customer_page, mock_api):
    from .helpers import assert_no_xss
    from .mock_api import XSS

    mock_api.state["addOns"][0]["name"] = "Big " + XSS
    mock_api.state["meals"][0]["addOnDetails"]["a1"]["description"] = "desc " + XSS
    page = customer_page
    page.open("/index.html")

    expect(page.locator(f"{CARD} .addon-description").first).to_contain_text("desc <img")
    assert_no_xss(page)


def test_addon_with_no_description_or_macros_is_left_off_the_meal(customer_page, mock_api):
    # The meal has a blank entry for the combo (both cleared on Manage menu).
    mock_api.state["meals"][0]["addOnDetails"]["a2"] = {
        "description": "", "macros": {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0},
    }
    page = customer_page
    page.open("/index.html")

    expect(page.locator(CARD).get_by_label("Large with Cauliflower Rice (+$4.00)", exact=True)).to_have_count(0)
    expect(page.locator(f"{CARD} .addon-choice")).to_have_count(2)  # Regular + Large


def test_addon_with_only_macros_is_still_offered(customer_page, mock_api):
    mock_api.state["meals"][0]["addOnDetails"]["a2"] = {
        "description": "", "macros": {"calories": 690, "proteinG": 0, "carbsG": 0, "fatG": 0},
    }
    page = customer_page
    page.open("/index.html")
    _choose(page, "Large with Cauliflower Rice (+$4.00)")

    expect(page.locator(f"{CARD} .macros")).to_contain_text("Large with Cauliflower Rice: 690 cal")


def test_addon_with_no_meal_details_and_no_default_description_is_left_off(customer_page, mock_api):
    mock_api.state["addOns"][1]["description"] = ""
    page = customer_page
    page.open("/index.html")

    expect(page.locator(f"{CARD} .addon-choice")).to_have_count(2)


def test_meal_with_no_addons_offered_has_no_choices(customer_page, mock_api):
    for addon in mock_api.state["addOns"]:
        addon["description"] = ""
    del mock_api.state["meals"][0]["addOnDetails"]
    page = customer_page
    page.open("/index.html")

    expect(page.locator(f"{CARD} .addon-choice")).to_have_count(0)
    expect(page.locator(f"{CARD} [role=radiogroup]")).to_have_count(0)
    page.click(ADD_M1)
    expect(page.locator("#cart-lines")).to_contain_text("Chicken Teriyaki Bowl")
