"""Extras & sauces (bought by quantity alongside meals), the weekly-menu
lines in How It Works, and the allergen & nutrition disclaimers on the
menu, My Orders and printed labels."""
from playwright.sync_api import expect

from .helpers import assert_no_xss, expect_toast

ADD_M1 = '.add-to-order[data-meal-id="m1"]'
EXTRA_ROW = '#extras-list .extra-row:has-text("{}")'
EXTRA_TABLE_ROW = '#extras-body tr[data-extra-id="{}"]'
DISCLAIMER_HEAD = "Our meals are prepared in a kitchen where common allergens"
PLEASE_NOTE = (
    "Meals are prepared in a kitchen that handles common allergens and cannot be guaranteed "
    "allergen-free. All nutritional information is estimated and may vary."
)
LABEL_DISCLAIMER = (
    "Prepared where common allergens are present. Cross-contact possible. Not guaranteed allergen-free. "
    "Nutrition values are estimates."
)


def _top(page, selector):
    return page.locator(selector).first.bounding_box()["y"]


def _more(page, name, times=1):
    for _ in range(times):
        page.locator(EXTRA_ROW.format(name)).get_by_role("button", name=f"One more {name}").click()


# ---------- How It Works ----------


def test_how_it_works_explains_the_weekly_menu_after_ordering(customer_page):
    page = customer_page
    page.open("/index.html")

    section = page.locator("#how-it-works")
    weekly = section.locator("p").filter(has_text="Our Weekly Menu:")
    expect(weekly).to_have_text("Our Weekly Menu: We offer four meals each week:")
    assert _top(page, "#how-it-works p:has-text('Ordering:')") < _top(page, "#how-it-works p:has-text('Our Weekly Menu:')")
    assert _top(page, "#how-it-works p:has-text('Our Weekly Menu:')") < _top(page, "#how-it-works p:has-text('Order Deadline:')")
    expect(section.locator("li").filter(has_text="2 Staple Meals")).to_have_text(
        "⭐️ 2 Staple Meals: One rotates every three months between our Chicken Fajita Protein Bowl, Taco Bowl, "
        "and Green Chicken Enchilada Bowl. The other is a chicken dish featuring a different seasoning each month."
    )
    expect(section.locator("li").filter(has_text="2 Rotating Meals")).to_have_text(
        "🔄 2 Rotating Meals: Two additional meals that change every week, giving you something new to try!"
    )


# ---------- Extras & sauces on the menu ----------


def test_extras_are_listed_below_the_meals_with_prices(customer_page):
    page = customer_page
    page.open("/index.html")

    section = page.locator("#extras-section")
    expect(section.locator("h2")).to_have_text("Extras & Sauces")
    # Alphabetical, and the hidden Sriracha isn't offered.
    expect(section.locator(".extra-name")).to_have_text(["BBQ Sauce", "Salsa Verde"])
    expect(section.locator(".extra-price")).to_have_text(["$0.25", "$0.25"])
    assert _top(page, "#meals") < _top(page, "#extras-section") < _top(page, "#allergen-disclaimer")


def test_extras_quantities_add_to_the_order_and_total(customer_page, mock_api):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    _more(page, "Salsa Verde", 2)
    _more(page, "BBQ Sauce", 3)

    lines = page.locator("#cart-lines .cart-extra")
    expect(lines).to_have_count(2)
    expect(lines.nth(0)).to_contain_text("BBQ Sauce")
    expect(lines.nth(0)).to_contain_text("$0.75")
    expect(lines.nth(1)).to_contain_text("Salsa Verde")
    expect(lines.nth(1)).to_contain_text("$0.50")
    expect(page.locator("#cart-total")).to_have_text("$14.24")  # 12.99 + 5 x 0.25

    # Lowering a quantity in the cart updates the menu's count too.
    lines.nth(0).get_by_role("button", name="One fewer BBQ Sauce").click()
    expect(page.locator(EXTRA_ROW.format("BBQ Sauce")).locator("input")).to_have_value("2")
    expect(page.locator("#cart-total")).to_have_text("$13.99")

    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")
    order = next(c for c in mock_api.writes() if c["path"] == "/orders")["body"]
    assert sorted(order["extras"], key=lambda e: e["extraId"]) == [
        {"extraId": "e1", "quantity": 2}, {"extraId": "e2", "quantity": 2},
    ]


def test_typing_a_quantity_of_zero_removes_the_extra(customer_page):
    page = customer_page
    page.open("/index.html")
    _more(page, "Salsa Verde", 2)
    page.locator(EXTRA_ROW.format("Salsa Verde")).locator("input").fill("0")
    page.locator(EXTRA_ROW.format("Salsa Verde")).locator("input").press("Tab")

    expect(page.locator("#cart-section")).to_be_hidden()


def test_extras_alone_cant_be_ordered(customer_page, mock_api):
    page = customer_page
    page.open("/index.html")
    _more(page, "Salsa Verde")
    page.click("#checkout-button")

    expect(page.locator("#checkout-error")).to_have_text(
        "Please add at least one meal - extras and sauces are added to a meal order."
    )
    assert not [c for c in mock_api.writes() if c["path"] == "/orders"]


def test_cancel_clears_the_extras_too(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    _more(page, "Salsa Verde", 2)
    page.click("#cancel-order-button")

    expect(page.locator("#cart-section")).to_be_hidden()
    expect(page.locator(EXTRA_ROW.format("Salsa Verde")).locator("input")).to_have_value("0")


def test_logged_out_visitors_see_extras_but_cant_pick_them(anonymous_page):
    page = anonymous_page
    page.open("/index.html")

    row = page.locator(EXTRA_ROW.format("Salsa Verde"))
    expect(row).to_be_visible()
    expect(row.get_by_role("button", name="One more Salsa Verde")).to_be_disabled()


def test_no_extras_section_while_ordering_is_closed(customer_page, mock_api):
    mock_api.state["schedule_open"] = False
    page = customer_page
    page.open("/index.html")

    expect(page.locator("#ordering-closed")).to_be_visible()
    expect(page.locator("#extras-section")).to_be_hidden()


def test_no_extras_section_when_there_are_none(customer_page, mock_api):
    mock_api.state["extras"] = []
    page = customer_page
    page.open("/index.html")

    expect(page.locator(".meal-card").first).to_be_visible()
    expect(page.locator("#extras-section")).to_be_hidden()


# ---------- Disclaimers ----------


def test_allergen_disclaimer_is_at_the_bottom_of_the_menu(customer_page):
    page = customer_page
    page.open("/index.html")

    disclaimer = page.locator("#allergen-disclaimer")
    expect(disclaimer.locator("h2")).to_have_text("Allergen & Nutrition Disclaimer")
    expect(disclaimer).to_contain_text(DISCLAIMER_HEAD)
    expect(disclaimer).to_contain_text(
        "Calories and macronutrients are estimates based on ingredients and portion sizes."
    )
    assert page.evaluate("document.querySelector('main').lastElementChild.id") == "allergen-disclaimer"


def test_my_orders_lists_extras_and_the_allergen_note(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    _more(page, "Salsa Verde", 2)
    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")

    card = page.locator(".order-card")
    expect(card.locator(".order-extra")).to_have_text(
        "2 x Salsa Verde - $0.50 (2 oz. tangy tomatillo salsa - 20 cal · 4g carbs each)"
    )
    note = card.locator(".allergen-note")
    expect(note.locator("strong")).to_have_text("Please Note")
    expect(note.locator("p")).to_have_text(PLEASE_NOTE)
    assert _top(page, ".order-deadline-note") < _top(page, ".allergen-note")


def test_editing_an_order_brings_its_extras_back(customer_page):
    page = customer_page
    page.open("/index.html")
    page.click(ADD_M1)
    _more(page, "BBQ Sauce", 3)
    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")

    page.locator(".order-card").get_by_role("button", name="Edit").click()
    page.wait_for_url("**/index.html")
    expect(page.locator("#cart-lines .cart-extra")).to_contain_text("BBQ Sauce")
    expect(page.locator(EXTRA_ROW.format("BBQ Sauce")).locator("input")).to_have_value("3")


# ---------- Admin: orders, prep list, labels ----------


def test_admin_orders_list_extras_under_the_meals(admin_page):
    page = admin_page
    page.open("/admin-orders.html")

    row = page.locator("#orders-body tr").filter(has_text="Chicken Teriyaki Bowl")
    expect(row.locator(".order-extra")).to_have_text("2 x Salsa Verde - $0.50")
    assert_no_xss(page)


def test_prep_list_totals_extras_for_the_date(admin_page, mock_api):
    mock_api.state["orders"][1]["extras"] = [
        {"extraId": "e1", "name": "Salsa Verde", "priceCents": 25, "quantity": 1},
        {"extraId": "e2", "name": "BBQ Sauce", "priceCents": 25, "quantity": 3},
    ]
    page = admin_page
    page.open("/admin-prep.html")

    rows = page.locator("#extras-body tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_text("BBQ Sauce3")
    expect(rows.nth(1)).to_have_text("Salsa Verde3")  # 2 + 1
    # Extras get no labels: 3 meals, 3 labels.
    expect(page.locator("#print-labels .label")).to_have_count(3)


def test_prep_list_hides_extras_when_none_were_ordered(admin_page, mock_api):
    mock_api.state["orders"][0]["extras"] = []
    page = admin_page
    page.open("/admin-prep.html")

    expect(page.locator("#prep-body tr").first).to_be_visible()
    expect(page.locator("#extras-card")).to_be_hidden()


def test_labels_end_with_the_allergen_disclaimer_and_still_fit(admin_page, mock_api):
    mock_api.state["meals"][0]["addOnDetails"]["a1"]["description"] = (
        "Includes 2 extra ounces of chicken thigh and 1 extra ounce of mixed vegetables."
    )
    page = admin_page
    page.open("/admin-prep.html")
    page.emulate_media(media="print")

    labels = page.locator("#print-labels .label")
    expect(labels.locator(".label-disclaimer")).to_have_text([LABEL_DISCLAIMER] * 3)
    fits = page.evaluate("[...document.querySelectorAll('#print-labels .label')].map(l => l.scrollHeight <= l.clientHeight)")
    assert fits == [True, True, True]
    # Pinned to the bottom edge of the label.
    gaps = page.evaluate("""[...document.querySelectorAll('#print-labels .label')].map(l => {
        const d = l.querySelector('.label-disclaimer').getBoundingClientRect(), b = l.getBoundingClientRect();
        return b.bottom - d.bottom; })""")
    assert all(gap < 20 for gap in gaps), gaps


def test_labels_leave_off_the_menu_markers(admin_page, mock_api):
    mock_api.state["orders"][0]["items"][0]["name"] = "⭐️Chicken Teriyaki Bowl"
    mock_api.state["orders"][1]["items"][0]["name"] = "🔄 Turkey Chili"
    page = admin_page
    page.open("/admin-prep.html")

    expect(page.locator("#print-labels .label-meal")).to_have_text(
        ["Chicken Teriyaki Bowl", "Chicken Teriyaki Bowl", "Turkey Chili"]
    )


# ---------- Admin: managing extras ----------


def test_manage_menu_adds_an_extra(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.fill("#extra-name", "Ranch")
    page.fill("#extra-price", "0.50")
    page.click("#extra-create-form button[type=submit]")

    expect_toast(page, 'Added "Ranch" to Extras & Sauces')
    assert mock_api.writes()[-1]["body"] == {
        "name": "Ranch", "description": "", "priceCents": 50,
        "macros": {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0},
    }
    expect(page.locator("#extras-body tr").filter(has_text="Ranch")).to_contain_text("$0.50")


def test_manage_menu_lists_hides_and_deletes_extras(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")

    expect(page.locator("#extras-body tr td:first-child strong")).to_have_text(["BBQ Sauce", "Salsa Verde", "Sriracha"])
    row = page.locator(EXTRA_TABLE_ROW.format("e1"))
    expect(row.get_by_role("button")).to_have_text(["Edit", "Hide", "Delete"])

    row.get_by_role("button", name="Hide").click()
    expect_toast(page, '"Salsa Verde" is now hidden')
    assert mock_api.writes()[-1]["body"] == {"available": False}

    page.locator(EXTRA_TABLE_ROW.format("e3")).get_by_role("button", name="Delete").click()
    expect(page.locator("#confirm-dialog-message")).to_have_text('Delete "Sriracha"?')
    page.keyboard.press("Enter")
    expect_toast(page, 'Deleted "Sriracha"')
    expect(page.locator(EXTRA_TABLE_ROW.format("e3"))).to_have_count(0)


def test_manage_menu_edits_an_extra(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    page.locator(EXTRA_TABLE_ROW.format("e2")).get_by_role("button", name="Edit").click()
    expect(page.locator("#edit-extra-name")).to_have_value("BBQ Sauce")
    expect(page.locator("#edit-extra-price")).to_have_value("0.25")
    page.fill("#edit-extra-price", "0.35")
    page.press("#edit-extra-name", "Enter")

    expect_toast(page, 'Saved changes to "BBQ Sauce"')
    assert mock_api.writes()[-1]["path"] == "/extras/e2"
    assert mock_api.writes()[-1]["body"] == {
        "name": "BBQ Sauce", "description": "", "priceCents": 35,
        "macros": {"calories": 0, "proteinG": 0, "carbsG": 0, "fatG": 0},
    }
    expect(page.locator(EXTRA_TABLE_ROW.format("e2"))).to_contain_text("$0.35")


# ---------- Staple / rotating markers ----------


def test_menu_marks_staple_and_rotating_meals(customer_page, mock_api):
    mock_api.state["meals"][1]["available"] = True  # Turkey Chili: this week only
    page = customer_page
    page.open("/index.html")

    staple = page.locator('.meal-card[data-meal-id="m1"] h3')
    rotating = page.locator('.meal-card[data-meal-id="m2"] h3')
    expect(staple).to_have_text("⭐️ Chicken Teriyaki Bowl")
    expect(rotating).to_have_text("🔄 Turkey Chili")
    expect(staple.get_by_role("img", name="Staple meal")).to_have_count(1)
    expect(rotating.get_by_role("img", name="Rotating meal")).to_have_count(1)



# ---------- Extras' descriptions and macros ----------


def test_menu_shows_an_extras_description_and_macros_only_when_it_has_them(customer_page):
    page = customer_page
    page.open("/index.html")

    salsa = page.locator(EXTRA_ROW.format("Salsa Verde"))
    expect(salsa.locator(".extra-description")).to_have_text("2 oz. tangy tomatillo salsa")
    expect(salsa.locator(".extra-macros")).to_have_text("20 cal · 4g carbs")
    bbq = page.locator(EXTRA_ROW.format("BBQ Sauce"))
    expect(bbq.locator(".extra-description, .extra-macros")).to_have_count(0)


def test_cart_shows_an_extras_macros(customer_page):
    page = customer_page
    page.open("/index.html")
    _more(page, "Salsa Verde", 2)
    _more(page, "BBQ Sauce")

    lines = page.locator("#cart-lines .cart-extra")
    expect(lines.filter(has_text="Salsa Verde")).to_contain_text("20 cal · 4g carbs each")
    expect(lines.filter(has_text="BBQ Sauce")).not_to_contain_text("cal")


def test_admin_orders_and_prep_list_show_extras_details(admin_page, mock_api):
    mock_api.state["orders"][0]["extras"][0].update(
        description="2 oz. tangy tomatillo salsa", macros={"calories": 20, "proteinG": 0, "carbsG": 4, "fatG": 0}
    )
    page = admin_page
    page.open("/admin-orders.html")
    extra = page.locator("#orders-body .order-extra")
    expect(extra).to_contain_text("2 oz. tangy tomatillo salsa")
    expect(extra).to_contain_text("20 cal · 4g carbs each")

    page.open("/admin-prep.html")
    row = page.locator("#extras-body tr")
    expect(row.locator(".cell-sub")).to_have_text(["2 oz. tangy tomatillo salsa", "20 cal · 4g carbs each"])


def test_manage_menu_saves_an_extras_description_and_macros(admin_page, mock_api):
    page = admin_page
    page.open("/admin.html")
    expect(page.locator(EXTRA_TABLE_ROW.format("e1"))).to_contain_text("2 oz. tangy tomatillo salsa")
    expect(page.locator(EXTRA_TABLE_ROW.format("e1"))).to_contain_text("20 cal · 4g carbs")

    page.locator(EXTRA_TABLE_ROW.format("e1")).get_by_role("button", name="Edit").click()
    expect(page.locator("#edit-extra-description")).to_have_value("2 oz. tangy tomatillo salsa")
    expect(page.locator("#edit-extra-calories")).to_have_value("20")
    page.fill("#edit-extra-description", "3 oz. cup")
    page.fill("#edit-extra-fat", "1")
    page.press("#edit-extra-name", "Enter")

    expect_toast(page, 'Saved changes to "Salsa Verde"')
    assert mock_api.writes()[-1]["body"] == {
        "name": "Salsa Verde", "description": "3 oz. cup", "priceCents": 25,
        "macros": {"calories": 20, "proteinG": 0, "carbsG": 4, "fatG": 1},
    }
