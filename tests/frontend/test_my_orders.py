"""My Orders page (orders.html): Edit / Cancel until the Friday deadline,
then a message to email instead. The API decides which (each order's
"changeable"); the deadline itself is unit-tested."""
from playwright.sync_api import expect

from .helpers import expect_toast

LOCKED_MESSAGE = (
    "Need to make changes to your order? Please email us no later than Friday at 2:00 PM. "
    "Changes and cancellations cannot be made after this deadline."
)
REFUSED = (
    "This order can no longer be changed or cancelled online - the deadline was Friday at 2:00 PM. "
    "Please email us."
)


def _place_order(page):
    page.open("/index.html")
    page.click('.add-to-order[data-meal-id="m1"]')
    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")


def test_edit_and_cancel_before_the_deadline(customer_page):
    page = customer_page
    _place_order(page)

    card = page.locator(".order-card")
    expect(card.get_by_role("button", name="Edit")).to_be_visible()
    expect(card.get_by_role("button", name="Cancel order")).to_be_visible()
    expect(card.locator(".order-deadline-note")).to_have_text(
        "You can edit or cancel this order until Friday at 2:00 PM."
    )
    expect(card.locator(".order-locked")).to_have_count(0)


def test_after_the_deadline_the_buttons_become_a_message(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    mock_api.state["changes_locked"] = True
    page.reload()

    card = page.locator(".order-card")
    expect(card.locator("button")).to_have_count(0)
    expect(card.locator(".order-locked")).to_have_text(LOCKED_MESSAGE)


def test_cancel_order_asks_first_then_cancels(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    page.click(".cancel-order")

    expect(page.locator("#confirm-dialog-message")).to_have_text("Cancel this order? This can't be undone.")
    expect(page.locator("#confirm-dialog-ok-button")).to_have_text("Cancel order")
    page.click("#confirm-dialog-ok-button")

    expect_toast(page, "Your order was cancelled")
    expect(page.locator(".order-card .status")).to_have_text("cancelled")
    expect(page.locator(".order-card button")).to_have_count(0)
    expect(page.locator(".order-card .order-locked")).to_have_count(0)
    call = mock_api.writes()[-1]
    assert (call["method"], call["body"]) == ("PUT", {"status": "cancelled"})


def test_changing_your_mind_keeps_the_order(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    page.click(".cancel-order")
    page.keyboard.press("Escape")

    expect(page.locator(".order-card .status")).to_have_text("placed")
    assert [c for c in mock_api.writes() if c["method"] == "PUT"] == []


def test_deadline_passing_while_the_page_is_open(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    mock_api.state["changes_locked"] = True  # the page still shows the buttons
    page.click(".cancel-order")
    page.click("#confirm-dialog-ok-button")

    expect_toast(page, REFUSED, error=True)
    expect(page.locator(".order-card .order-locked")).to_have_text(LOCKED_MESSAGE)
    expect(page.locator(".order-card .status")).to_have_text("placed")


def test_edit_after_the_deadline_stays_on_the_page(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    mock_api.state["changes_locked"] = True
    page.click(".edit-order")

    expect_toast(page, REFUSED, error=True)
    assert page.url.endswith("/orders.html")
    assert page.evaluate("sessionStorage.getItem('editOrderCart')") is None


def test_edit_reloads_the_order_into_the_menu(customer_page):
    page = customer_page
    _place_order(page)
    page.click(".edit-order")

    page.wait_for_url("**/index.html")
    expect(page.locator("#edit-notice")).to_be_visible()
    expect(page.locator("#cart-lines")).to_contain_text("Chicken Teriyaki Bowl")


def test_edit_tells_the_api_the_new_order_replaces_the_old_one(customer_page, mock_api):
    # So the admin gets one "order updated" email, not a cancel and a new order.
    page = customer_page
    _place_order(page)
    old_id = mock_api.writes()[-1]
    page.click(".edit-order")
    page.wait_for_url("**/index.html")
    cancel = mock_api.writes()[-1]
    assert cancel["method"] == "PUT"
    assert cancel["body"] == {"status": "cancelled", "forEdit": True}
    order_id = cancel["path"].rsplit("/", 1)[1]

    page.click("#checkout-button")
    page.wait_for_url("**/orders.html")
    placed = mock_api.writes()[-1]
    assert placed["method"] == "POST"
    assert placed["body"]["replacesOrderId"] == order_id
    assert old_id["body"].get("replacesOrderId") is None


def test_cancel_is_not_marked_as_an_edit(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    page.click(".cancel-order")
    page.click("#confirm-dialog-ok-button")

    expect_toast(page, "Your order was cancelled")
    assert mock_api.writes()[-1]["body"] == {"status": "cancelled"}


def test_my_orders_shows_whether_payment_was_received(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    expect(page.locator(".order-card .order-payment")).to_have_text(
        "Payment not received yet - Venmo @lgraffeo88, or cash."
    )
    order = mock_api.state["placed"][0]
    order.update(payment="venmo", amountPaidCents=order["totalCents"])
    page.reload()
    expect(page.locator(".order-card .order-payment")).to_have_text("Payment received (Venmo) - thank you!")
    order["amountPaidCents"] = 500
    page.reload()
    expect(page.locator(".order-card .order-payment")).to_have_text("Paid $5.00 (Venmo) - $7.99 still due.")
    order["amountPaidCents"] = 2000
    page.reload()
    expect(page.locator(".order-card .order-payment")).to_have_text("Paid $20.00 (Venmo) - we'll refund $7.01.")


def test_no_payment_line_on_a_cancelled_order(customer_page, mock_api):
    page = customer_page
    _place_order(page)
    mock_api.state["placed"][0]["status"] = "cancelled"
    page.reload()
    expect(page.locator(".order-card")).to_be_visible()
    expect(page.locator(".order-card .order-payment")).to_have_count(0)
