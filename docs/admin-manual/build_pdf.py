"""Renders manual.html to frontend/admin-manual.pdf with Chrome - served by
the site and linked as "Manual" in the admin menu (see frontend/js/nav.js),
so the next deploy publishes it.

Run from the repo root:
    python docs/admin-manual/screenshots.py docs/admin-manual/img   # refresh screenshots (sample data)
    python docs/admin-manual/build_pdf.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "frontend" / "admin-manual.pdf"

FOOTER = """<div style="width:100%; font-size:8px; color:#777; padding:0 0.75in;
  display:flex; justify-content:space-between; font-family:Helvetica, sans-serif;">
  <span>GTX Meals &middot; Admin Manual</span><span class="pageNumber"></span></div>"""

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page()
    page.goto((HERE / "manual.html").as_uri())
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.pdf(path=str(OUT), prefer_css_page_size=True, print_background=True,
             display_header_footer=True, header_template="<span></span>", footer_template=FOOTER)
    browser.close()
print(OUT)
