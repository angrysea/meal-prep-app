"""Assertions shared by the frontend tests."""
import re

from playwright.sync_api import expect


def expect_toast(page, text, error=False):
    toast = page.locator("#toast")
    expect(toast).to_have_class(re.compile(r"\bvisible\b"))
    expect(toast).to_have_text(text)
    if error:
        expect(toast).to_have_class(re.compile(r"\btoast-error\b"))
    else:
        expect(toast).not_to_have_class(re.compile(r"\btoast-error\b"))


def assert_no_xss(page):
    assert page.evaluate("!!window.__xss") is False, "customer-entered markup was executed"
