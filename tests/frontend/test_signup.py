"""Sign-up on login.html: the profile (phone, address, contact preference,
opt-out) is collected at sign-up and saved once the account is confirmed.

The page calls Cognito's public API straight from the browser, so these
tests intercept those requests with FakeCognito instead of a real pool.
"""
import base64
import json
import re

import pytest
from playwright.sync_api import expect

COGNITO_URL = re.compile(r"https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/.*")
GOOD_CODE = "123456"


def _fake_id_token(sub, email):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'sub': sub, 'email': email})}.sig"


class FakeCognito:
    def __init__(self):
        self.calls = []
        self.registered = {}

    def actions(self):
        return [action for action, _ in self.calls]

    def handle(self, route):
        action = route.request.headers["x-amz-target"].split(".")[-1]
        body = json.loads(route.request.post_data or "{}")
        self.calls.append((action, body))

        def respond(status, payload):
            route.fulfill(status=status, content_type="application/x-amz-json-1.1", body=json.dumps(payload))

        if action == "SignUp":
            self.registered[body["Username"]] = body["Password"]
            return respond(200, {"UserConfirmed": False, "UserSub": "new-sub"})
        if action == "ConfirmSignUp":
            if body["ConfirmationCode"] != GOOD_CODE:
                return respond(400, {"__type": "CodeMismatchException",
                                     "message": "Invalid verification code provided, please try again."})
            return respond(200, {})
        if action == "InitiateAuth":
            email = body["AuthParameters"]["USERNAME"]
            if self.registered.get(email) != body["AuthParameters"]["PASSWORD"]:
                return respond(400, {"__type": "NotAuthorizedException", "message": "Incorrect username or password."})
            return respond(200, {"AuthenticationResult": {
                "IdToken": _fake_id_token("new-sub", email), "RefreshToken": "refresh", "ExpiresIn": 3600,
            }})
        return respond(200, {})


@pytest.fixture
def signup_page(anonymous_page):
    page = anonymous_page
    cognito = FakeCognito()
    page.route(COGNITO_URL, cognito.handle)
    page.cognito = cognito
    page.open("/login.html")
    page.click("#tab-signup")
    yield page
    assert page.errors == [], f"JavaScript errors on the page: {page.errors}"


def _fill_signup(page, *, phone="5551234567", text=False, unsubscribed=False, confirm="s3cret-pass"):
    page.fill("#signup-name", "Pat Lee")
    page.fill("#signup-email", "pat@example.com")
    page.fill("#signup-phone", phone)
    page.fill("#signup-address", "9 Elm St")
    page.fill("#signup-password", "s3cret-pass")
    page.fill("#signup-password-confirm", confirm)
    if text:
        page.check("#signup-contact-text")
    if unsubscribed:
        page.check("#signup-unsubscribed")


def _profile_writes(mock_api):
    return [c for c in mock_api.writes() if c["path"] == "/profile"]


def test_signup_saves_full_profile_after_confirming(signup_page, mock_api):
    page = signup_page
    _fill_signup(page, text=True)
    expect(page.locator("#signup-phone")).to_have_value("(555) 123-4567")
    page.click("#signup-button")

    expect(page.locator("#confirm-form")).to_be_visible()
    action, body = page.cognito.calls[0]
    assert action == "SignUp"
    assert {"Name": "name", "Value": "Pat Lee"} in body["UserAttributes"]

    page.fill("#confirm-code", GOOD_CODE)
    page.press("#confirm-code", "Enter")

    # Signed in automatically - no second password entry - and sent home.
    page.wait_for_url("**/index.html")
    assert page.cognito.actions() == ["SignUp", "ConfirmSignUp", "InitiateAuth"]
    [write] = _profile_writes(mock_api)
    assert write["body"] == {
        "name": "Pat Lee",
        "email": "pat@example.com",
        "phone": "(555) 123-4567",
        "address": "9 Elm St",
        "preferredContact": "text",
        "unsubscribed": False,
    }
    assert write["auth"] == _fake_id_token("new-sub", "pat@example.com")
    assert page.evaluate("sessionStorage.getItem('mealprep.pendingProfile')") is None


def test_signup_honors_redirect_after_confirming(anonymous_page, mock_api):
    page = anonymous_page
    cognito = FakeCognito()
    page.route(COGNITO_URL, cognito.handle)
    page.open("/login.html?redirect=/account.html")
    page.click("#tab-signup")
    _fill_signup(page)
    page.click("#signup-button")
    page.fill("#confirm-code", GOOD_CODE)
    page.click("#confirm-button")

    page.wait_for_url("**/account.html")


def test_opting_out_of_notifications(signup_page, mock_api):
    page = signup_page
    _fill_signup(page, unsubscribed=True)
    page.click("#signup-button")
    page.fill("#confirm-code", GOOD_CODE)
    page.click("#confirm-button")

    page.wait_for_url("**/index.html")
    body = _profile_writes(mock_api)[0]["body"]
    assert (body["preferredContact"], body["unsubscribed"]) == ("email", True)


def test_sms_consent_shows_only_when_text_is_chosen(signup_page):
    page = signup_page
    consent = page.locator("#signup-sms-consent")
    expect(consent).to_be_hidden()

    page.check("#signup-contact-text")
    expect(consent).to_be_visible()
    expect(consent).to_contain_text("Reply STOP to opt out")

    page.check("#signup-contact-email")
    expect(consent).to_be_hidden()


def test_text_requires_a_phone_number(signup_page):
    page = signup_page
    _fill_signup(page, phone="", text=True)
    page.click("#signup-button")

    expect(page.locator("#signup-error")).to_have_text(
        "Please enter a phone number so we can text you, or choose Email."
    )
    assert page.cognito.calls == []


def test_email_only_signup_doesnt_need_a_phone(signup_page, mock_api):
    page = signup_page
    _fill_signup(page, phone="")
    page.click("#signup-button")

    expect(page.locator("#confirm-form")).to_be_visible()


def test_partial_phone_number_is_rejected(signup_page):
    page = signup_page
    _fill_signup(page, phone="555123")
    page.click("#signup-button")

    expect(page.locator("#signup-error")).to_have_text("Please enter a 10-digit phone number.")
    assert page.cognito.calls == []


def test_enter_in_any_field_submits_signup(signup_page):
    page = signup_page
    _fill_signup(page)
    page.press("#signup-address", "Enter")

    expect(page.locator("#confirm-form")).to_be_visible()
    assert page.cognito.actions() == ["SignUp"]


def test_wrong_code_shows_error_then_correct_code_works(signup_page, mock_api):
    page = signup_page
    _fill_signup(page)
    page.click("#signup-button")
    page.fill("#confirm-code", "000000")
    page.click("#confirm-button")

    expect(page.locator("#confirm-error")).to_have_text("Invalid verification code provided, please try again.")
    assert _profile_writes(mock_api) == []

    page.fill("#confirm-code", GOOD_CODE)
    page.click("#confirm-button")
    page.wait_for_url("**/index.html")
    assert len(_profile_writes(mock_api)) == 1


def test_details_saved_at_first_login_after_a_reload(signup_page, mock_api):
    # Reloading before confirming loses the in-memory password, so the
    # automatic sign-in can't happen - the details wait in sessionStorage
    # for the first manual login instead.
    page = signup_page
    _fill_signup(page, text=True)
    page.click("#signup-button")
    expect(page.locator("#confirm-form")).to_be_visible()

    page.reload()
    page.fill("#login-email", "pat@example.com")
    page.fill("#login-password", "s3cret-pass")
    page.click("#login-button")

    page.wait_for_url("**/index.html")
    [write] = _profile_writes(mock_api)
    assert write["body"]["phone"] == "(555) 123-4567"
    assert write["body"]["preferredContact"] == "text"
    assert page.evaluate("sessionStorage.getItem('mealprep.pendingProfile')") is None


def test_ordinary_login_saves_no_profile(signup_page, mock_api):
    page = signup_page
    page.cognito.registered["old@example.com"] = "pw-12345678"
    page.click("#tab-login")
    page.fill("#login-email", "old@example.com")
    page.fill("#login-password", "pw-12345678")
    page.click("#login-button")

    page.wait_for_url("**/index.html")
    assert _profile_writes(mock_api) == []


def test_failed_profile_save_points_to_my_account(signup_page, mock_api):
    mock_api.fail("PUT", "/profile", status=500, message="Internal error")
    page = signup_page
    _fill_signup(page)
    page.click("#signup-button")
    page.fill("#confirm-code", GOOD_CODE)
    page.click("#confirm-button")

    error = page.locator("#confirm-error")
    expect(error).to_contain_text("Your account is ready, but we couldn't save your details (Internal error).")
    expect(error.locator("a")).to_have_attribute("href", "/account.html")


# ---------- Confirm password and Show/Hide ----------


def test_mismatched_passwords_stop_signup(signup_page):
    page = signup_page
    _fill_signup(page, confirm="s3cret-pas")
    page.click("#signup-button")

    expect(page.locator("#signup-error")).to_have_text("Your passwords don't match.")
    assert page.cognito.calls == []
    expect(page.locator("#confirm-form")).to_be_hidden()


def test_password_match_feedback_updates_as_you_type(signup_page):
    page = signup_page
    match = page.locator("#signup-password-match")
    page.fill("#signup-password", "s3cret-pass")
    expect(match).to_be_hidden()  # nothing typed in the confirmation yet

    page.fill("#signup-password-confirm", "s3cret")
    expect(match).to_have_text("Passwords don't match.")
    expect(match).to_have_class(re.compile(r"\berror\b"))

    page.fill("#signup-password-confirm", "s3cret-pass")
    expect(match).to_have_text("Passwords match.")
    expect(match).not_to_have_class(re.compile(r"\berror\b"))

    # Editing the first password re-checks too.
    page.fill("#signup-password", "s3cret-pass!")
    expect(match).to_have_text("Passwords don't match.")


def test_eye_button_reveals_password_in_plain_text(signup_page):
    page = signup_page
    password = page.locator("#signup-password")
    toggle = page.locator('button.password-toggle[aria-controls="signup-password"]')
    password.fill("s3cret-pass")
    expect(password).to_have_attribute("type", "password")
    # An icon, not a text label - open eye while hidden.
    expect(toggle).to_have_text("")
    expect(toggle.locator(".icon-eye")).to_be_visible()
    expect(toggle.locator(".icon-eye-off")).to_be_hidden()
    expect(toggle).to_have_accessible_name("Show password")

    toggle.click()
    expect(password).to_have_attribute("type", "text")
    expect(password).to_have_value("s3cret-pass")
    expect(toggle.locator(".icon-eye-off")).to_be_visible()
    expect(toggle.locator(".icon-eye")).to_be_hidden()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(toggle).to_have_accessible_name("Hide password")
    expect(toggle).to_have_attribute("title", "Hide password")

    toggle.click()
    expect(password).to_have_attribute("type", "password")
    expect(toggle.locator(".icon-eye")).to_be_visible()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    expect(toggle).to_have_accessible_name("Show password")


def test_eye_button_works_from_the_keyboard(signup_page):
    page = signup_page
    page.focus('button.password-toggle[aria-controls="signup-password-confirm"]')
    page.keyboard.press("Space")

    expect(page.locator("#signup-password-confirm")).to_have_attribute("type", "text")


def test_each_password_box_toggles_independently(signup_page):
    page = signup_page
    page.click('button.password-toggle[aria-controls="signup-password-confirm"]')

    expect(page.locator("#signup-password-confirm")).to_have_attribute("type", "text")
    expect(page.locator("#signup-password")).to_have_attribute("type", "password")


def test_show_button_doesnt_submit_the_form(signup_page):
    page = signup_page
    _fill_signup(page)
    page.click('button.password-toggle[aria-controls="signup-password"]')

    assert page.cognito.calls == []
    expect(page.locator("#signup-form")).to_be_visible()


def test_signup_works_with_passwords_shown(signup_page, mock_api):
    page = signup_page
    _fill_signup(page)
    for target in ("signup-password", "signup-password-confirm"):
        page.click(f'button.password-toggle[aria-controls="{target}"]')
    page.click("#signup-button")

    expect(page.locator("#confirm-form")).to_be_visible()
    action, body = page.cognito.calls[0]
    assert (action, body["Password"]) == ("SignUp", "s3cret-pass")


# ---------- Login password eye button ----------


def test_login_eye_button_shows_password_in_plain_text(signup_page):
    page = signup_page
    page.click("#tab-login")
    password = page.locator("#login-password")
    toggle = page.locator('button.password-toggle[aria-controls="login-password"]')
    password.fill("pw-12345678")

    expect(toggle.locator(".icon-eye")).to_be_visible()
    expect(toggle).to_have_accessible_name("Show password")
    toggle.click()
    expect(password).to_have_attribute("type", "text")
    expect(password).to_have_value("pw-12345678")
    expect(toggle.locator(".icon-eye-off")).to_be_visible()
    expect(toggle).to_have_accessible_name("Hide password")

    toggle.click()
    expect(password).to_have_attribute("type", "password")


def test_login_works_with_password_shown(signup_page):
    page = signup_page
    page.cognito.registered["old@example.com"] = "pw-12345678"
    page.click("#tab-login")
    page.fill("#login-email", "old@example.com")
    page.fill("#login-password", "pw-12345678")
    page.click('button.password-toggle[aria-controls="login-password"]')
    page.click("#login-button")

    page.wait_for_url("**/index.html")
    assert ("InitiateAuth", "pw-12345678") in [
        (a, b.get("AuthParameters", {}).get("PASSWORD")) for a, b in page.cognito.calls
    ]


def test_every_password_box_gets_an_eye_button(signup_page):
    page = signup_page
    for target in ("login-password", "signup-password", "signup-password-confirm"):
        toggle = page.locator(f'button.password-toggle[aria-controls="{target}"]')
        expect(toggle.locator("svg")).to_have_count(2)
