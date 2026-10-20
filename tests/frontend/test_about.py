"""About Us page (about.html)."""
from playwright.sync_api import expect


def test_about_us_has_the_new_text(customer_page):
    page = customer_page
    page.open("/about.html")

    expect(page.locator("h1")).to_have_text("Meet Christian & Liz")
    headings = page.locator("main h2").all_text_contents()
    assert headings == ["Meet Christian", "Meet Liz", "How It All Started", "Let Us Take Cooking Off Your To-Do List!"]
    expect(page.locator("main")).to_contain_text("The Gym TX Meals officially began in March 2026.")
    # The old text is gone.
    expect(page.locator("main")).not_to_contain_text("not a factory")


def test_browse_our_menu_goes_to_the_ordering_page(customer_page):
    page = customer_page
    page.open("/about.html")
    button = page.get_by_role("link", name="Browse Our Menu")
    # It's the last thing on the page.
    assert button.evaluate("el => el.closest('section') === document.querySelector('main').lastElementChild")
    button.click()

    page.wait_for_url("**/index.html")
