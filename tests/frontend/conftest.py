"""Fixtures for browser tests of the static frontend.

Each test gets a fresh browser context (clean localStorage) against a mock
API reset to seed data (see mock_api.py), driving the system's installed
Google Chrome headlessly. Without Chrome installed these tests skip rather
than fail, so `make test` still works everywhere.
"""
import base64
import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .mock_api import MockApi

DESKTOP = {"width": 1440, "height": 900}
# Screenshots of failed tests land here (gitignored).
RESULTS_DIR = Path(__file__).resolve().parents[2] / "test-results"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    # Expose each phase's outcome to fixtures, so page fixtures can tell
    # at teardown whether the test failed.
    report = yield
    setattr(item, f"report_{report.when}", report)
    return report


def _screenshot_if_failed(request, page):
    report = getattr(request.node, "report_call", None)
    if report is not None and report.failed:
        RESULTS_DIR.mkdir(exist_ok=True)
        name = request.node.nodeid.split("::", 1)[-1].replace("/", "_")
        page.screenshot(path=str(RESULTS_DIR / f"{name}.png"), full_page=True)


def _b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _session_json(groups):
    # Only the payload matters - the frontend decodes it for the
    # cognito:groups claim and never verifies the signature (the API does).
    id_token = f"{_b64url({'alg': 'none'})}.{_b64url({'cognito:groups': groups, 'email': 'admin@example.com'})}.sig"
    return json.dumps({"idToken": id_token, "refreshToken": None, "expiresAt": int(time.time() * 1000) + 86_400_000})


@pytest.fixture(scope="session")
def mock_api():
    api = MockApi().start()
    yield api
    api.stop()


@pytest.fixture(autouse=True)
def _fresh_api_state(mock_api):
    mock_api.reset()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError as err:
            pytest.skip(f"Google Chrome isn't available for frontend tests: {err.message.splitlines()[0]}")
        yield browser
        browser.close()


def _new_page(browser, mock_api, session_json):
    context = browser.new_context(viewport=DESKTOP)
    init = "window.print = () => { window.__printed = (window.__printed || 0) + 1; };"
    if session_json:
        init += f"localStorage.setItem('mealprep.session', {json.dumps(session_json)});"
    context.add_init_script(init)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda err: page.errors.append(str(err)))
    # A request that never got a response (dropped connection) surfaces as
    # a clear failure instead of a 30s locator timeout. Chrome also fires
    # requestfailed (ERR_ABORTED) after a successful 204 response, so only
    # count requests that never got a response at all.
    responded = set()
    failed = []
    page.on("response", lambda res: responded.add(res.request))
    page.on("requestfailed", lambda req: req.url.startswith(mock_api.base_url) and failed.append(req))
    page.unanswered_requests = lambda: [
        f"request failed: {req.method} {req.url} ({req.failure})" for req in failed if req not in responded
    ]

    def open_path(path):
        page.goto(f"{mock_api.base_url}{path}")
        page.wait_for_load_state("networkidle")

    page.open = open_path
    return context, page


@pytest.fixture
def admin_page(request, browser, mock_api):
    context, page = _new_page(browser, mock_api, _session_json(["Admins"]))
    yield page
    _screenshot_if_failed(request, page)
    errors = page.errors + page.unanswered_requests()
    context.close()
    assert errors == [], f"JavaScript errors on the page: {errors}"


@pytest.fixture
def customer_page(request, browser, mock_api):
    context, page = _new_page(browser, mock_api, _session_json([]))
    yield page
    _screenshot_if_failed(request, page)
    context.close()


@pytest.fixture
def anonymous_page(request, browser, mock_api):
    context, page = _new_page(browser, mock_api, None)
    yield page
    _screenshot_if_failed(request, page)
    context.close()
