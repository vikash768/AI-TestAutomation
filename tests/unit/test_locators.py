"""Regression: a menu item rendered in a portal must never resolve to the sidebar's Log out button."""
import pytest

from qaplatform.config import Settings
from qaplatform.execute.smoke import SmokeRunner
from qaplatform.explore.scripts import COLLECT_INTERACTIVE_JS, FORMS_JS

HTML = """<!doctype html><html><body>
<div id="root"><div><aside><div><a href="/a">A</a></div><div><button id="noise" onclick="window.__hit='logout'">Log out</button></div></aside>
<main><div><div><div><div><div><div><div><div><div><div><form><label for="q">Q</label><input id="q" required><button>Go</button></form></div></div></div></div></div></div></div></div></div></div></main></div></div>
<div><div><button onclick="window.__hit='edit'">Edit Role</button></div></div>
</body></html>"""


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    pg = b.new_context().new_page()
    pg.set_content(HTML.replace("<button id=\"noise\"", "<button"))
    yield pg
    b.close()
    pw.stop()


def test_selectors_are_anchored_and_unique(page):
    els = page.evaluate(COLLECT_INTERACTIVE_JS, 100)
    for e in els:
        assert e["selector"].startswith(("body >", "[id=")), e["selector"]
        assert page.locator(e["selector"]).count() == 1, e["selector"]
    edit = next(e for e in els if e["name"] == "Edit Role")
    page.locator(edit["selector"]).click()
    assert page.evaluate("window.__hit") == "edit"
    for f in page.evaluate(FORMS_JS):
        for fld in f["fields"]:
            assert page.locator(fld["selector"]).count() == 1


def test_locate_rejects_a_selector_that_lands_on_another_control(page, tmp_path):
    runner = SmokeRunner(Settings(), "https://x", "", tmp_path, {}, lambda *a, **k: None)
    runner.page = page
    page.evaluate("window.__hit = null")
    # the old unanchored selector shape: matches the sidebar's Log out first
    stale = "div:nth-of-type(2) > button:nth-of-type(1)"
    assert page.locator(stale).first.inner_text() == "Log out"
    loc = runner.locate(stale, "button", "Edit Role")
    loc.click()
    assert page.evaluate("window.__hit") == "edit"
