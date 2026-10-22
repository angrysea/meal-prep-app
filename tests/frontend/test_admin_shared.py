"""Behavior shared across pages: the admin guard, the desktop-only admin
layout, and the admin's send-menu action on the public menu flyer."""
import pytest
from playwright.sync_api import expect

from .helpers import expect_toast

ADMIN_PAGES = ["/admin.html", "/admin-orders.html", "/admin-prep.html", "/customers.html"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_require_login(anonymous_page, path):
    page = anonymous_page
    page.open(path)

    expect(page.locator("#admin-guard")).to_contain_text("log in")
    expect(page.locator("#admin-content")).to_be_hidden()


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_reject_non_admins(customer_page, path):
    page = customer_page
    page.open(path)

    expect(page.locator("#admin-guard")).to_have_text("Your account doesn't have admin access.")
    expect(page.locator("#admin-content")).to_be_hidden()


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_keep_desktop_layout_in_a_narrow_window(admin_page, path):
    # Admin screens are desktop-only: below their min-width they scroll
    # sideways instead of reflowing into the phone layout.
    page = admin_page
    page.set_viewport_size({"width": 900, "height": 800})
    page.open(path)

    assert page.evaluate("document.body.scrollWidth") >= 1100
    expect(page.locator("#nav-toggle")).to_be_hidden()


def test_menu_flyer_send_defaults_to_send_on_enter(admin_page, mock_api):
    page = admin_page
    page.open("/menu-flyer.html?period=weekly")
    page.click("#text-button")

    expect(page.locator("#text-send-button")).to_be_focused()
    page.keyboard.press("Enter")

    expect_toast(page, "Menu sent to 1 by email, 0 by text, skipped 1 unsubscribed.")
    call = mock_api.writes()[-1]
    assert call["path"] == "/menu/send"
    assert call["body"]["subject"] == "GTX Meals - This week's menu is up!"


def test_menu_flyer_send_failure_shows_error_toast(admin_page, mock_api):
    mock_api.fail("POST", "/menu/send", message="no sender address is configured")
    page = admin_page
    page.open("/menu-flyer.html?period=weekly")
    page.click("#text-button")
    page.click("#text-send-button")

    expect_toast(page, "no sender address is configured", error=True)


def test_menu_flyer_hides_send_button_from_customers(customer_page):
    page = customer_page
    page.open("/menu-flyer.html?period=weekly")

    expect(page.locator("#text-button")).to_be_hidden()


def test_customer_menu_page_add_to_cart_toast(customer_page):
    # index.html uses the same shared toast module as the admin pages.
    page = customer_page
    page.open("/index.html")
    page.click('.add-to-order[data-meal-id="m1"]')

    expect_toast(page, "Chicken Teriyaki Bowl added to your order")


def test_admin_menu_links_to_the_manual(admin_page):
    page = admin_page
    page.open("/admin.html")

    links = [l.strip() for l in page.locator("#nav-links a").all_text_contents()]
    assert links == ["Menu", "Builder", "Orders", "Prep List", "Customers", "Manual", "Log out"]
    manual = page.locator("#nav-links a", has_text="Manual")
    expect(manual).to_have_attribute("href", "/admin-manual.pdf")
    expect(manual).to_have_attribute("target", "_blank")
    response = page.request.get(page.url.split("/admin.html")[0] + "/admin-manual.pdf")
    assert response.ok and response.body()[:4] == b"%PDF"


def test_customers_do_not_see_the_manual_link(customer_page):
    page = customer_page
    page.open("/index.html")
    expect(page.locator("#nav-links a", has_text="Manual")).to_have_count(0)
