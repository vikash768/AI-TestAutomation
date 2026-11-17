"""Executor and page scripts against a static page in headless Chromium."""
from pathlib import Path

import pytest

from qaplatform.config import Settings
from qaplatform.execute.actions import Action, ActionType, Target
from qaplatform.execute.executor import PlaywrightExecutor
from qaplatform.explore.scripts import COLLECT_INTERACTIVE_JS, LAYOUT_CHECK_JS, PAGE_STATE_JS

HTML = """<!doctype html><html><body style="margin:0">
<div id="root">
<nav><a href="/dashboard" id="dash">Dashboard</a><a href="/users">Users</a></nav>
<form><label for="email">Email</label><input id="email" type="email" required>
<input type="password" placeholder="Password"><button type="submit">Sign in</button></form>
<button aria-label="Show password"><svg></svg></button>
<button id="toggle" onclick="document.getElementById('msg').textContent='clicked'">Toggle</button>
<button role="switch" aria-checked="false" style="width:18px;height:18px">tiny</button>
<p id="msg"></p>
<div style="width:1600px;height:10px;background:red">wide</div>
<div role="dialog" hidden>hidden dialog</div>
</div></body></html>"""


@pytest.fixture(scope="module")
def page():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {exc}")
    ctx = browser.new_context(viewport={"width": 400, "height": 700})
    pg = ctx.new_page()
    pg.set_content(HTML)
    yield pg
    browser.close()
    pw.stop()


def test_collect_interactive(page):
    els = page.evaluate(COLLECT_INTERACTIVE_JS, 100)
    names = {(e["role"], e["name"]) for e in els}
    assert ("link", "Dashboard") in names and ("textbox", "Email") in names and ("button", "Sign in") in names
    assert ("button", "Show password") in names and ("switch", "tiny") in names
    dash = next(e for e in els if e["name"] == "Dashboard")
    assert dash["selector"] == '[id="dash"]' and dash["href"].endswith("/dashboard")
    assert all(not e["in_dialog"] for e in els)


def test_layout_check_flags_overflow_and_small_targets(page):
    layout = page.evaluate(LAYOUT_CHECK_JS, {"checkTargets": True})
    assert layout["horizontalOverflow"] is True
    assert any(d["text"] == "wide" for d in layout["overflowing"])
    assert any(d["text"] == "tiny" for d in layout["smallTargets"])


def test_page_state_and_executor(page, tmp_path):
    before = page.evaluate(PAGE_STATE_JS)
    ex = PlaywrightExecutor(page, Settings(settle_ms=50, action_timeout_ms=2000), tmp_path)
    out = ex.run([
        Action(ActionType.FILL, target=Target(selector="#email"), value="a@b.c"),
        Action(ActionType.EXPECT_VALUE, target=Target(selector="#email"), value="a@b.c"),
        Action(ActionType.CLICK, target=Target(role="button", name="Toggle")),
        Action(ActionType.EXPECT_TEXT, target=Target(selector="#msg"), value="clicked"),
        Action(ActionType.EXPECT_VISIBLE, target=Target(role="switch")),
    ])
    assert out.ok, out.error
    after = page.evaluate(PAGE_STATE_JS)
    assert after["domHash"] != before["domHash"]
    bad = ex.run([Action(ActionType.EXPECT_TEXT, value="definitely not here")])
    assert not bad.ok and bad.classification == "VALIDATION_FAILURE"
    missing = ex.run([Action(ActionType.CLICK, target=Target(selector="#nope"), timeout_ms=300)])
    assert not missing.ok and missing.classification in ("LOCATOR_ERROR", "TIMEOUT")
