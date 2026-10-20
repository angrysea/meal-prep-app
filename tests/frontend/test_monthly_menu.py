"""Monthly Menu page (menu-flyer.html?period=monthly): a sneak peek of
upcoming meals, without nutrition info."""
from playwright.sync_api import expect

MONTHLY = "/menu-flyer.html?period=monthly"


def test_back_to_main_menu_goes_to_the_ordering_page(customer_page):
    page = customer_page
    page.open(MONTHLY)
    back = page.get_by_role("link", name="Back to Main Menu")
    expect(back).to_be_visible()
    back.click()

    page.wait_for_url("**/index.html")


def test_back_button_works_when_opened_directly(anonymous_page):
    # e.g. from a texted link, with no page to go "back" to.
    page = anonymous_page
    page.open(MONTHLY)

    expect(page.locator("#back-to-menu")).to_have_attribute("href", "/index.html")
    expect(page.locator("#cancel-button")).to_have_count(0)


def test_monthly_menu_has_no_macros(customer_page):
    page = customer_page
    page.open(MONTHLY)

    expect(page.locator(".item-name").first).to_contain_text("Chicken Teriyaki Bowl")
    expect(page.locator(".item-macros")).to_have_count(0)
    expect(page.locator("#menu-body")).not_to_contain_text("cal")


def test_weekly_flyer_keeps_macros(admin_page):
    page = admin_page
    page.open("/menu-flyer.html?period=weekly")

    expect(page.locator(".item-macros").first).to_contain_text("550")


def test_back_button_is_hidden_when_printing(customer_page):
    page = customer_page
    page.open(MONTHLY)
    page.emulate_media(media="print")

    expect(page.locator("#back-to-menu")).to_be_hidden()
