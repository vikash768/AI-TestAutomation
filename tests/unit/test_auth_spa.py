"""Auth checks on a router-less SPA: Enter submits and the URL never changes."""
import pytest

from qaplatform.execute.forms import _spa_signed_in
from qaplatform.explore.scripts import FEEDBACK_JS

LOGIN_SPA = """<!doctype html><html><body><div id="root"></div><script>
const root=document.getElementById('root');
function loginView(err){root.innerHTML=`<form id="f" novalidate><input id="e" type="email"><input id="p" type="password">
 ${err?'<p class="field-error">Invalid email or password</p>':''}<button type="submit">Sign In</button></form>`;
 document.getElementById('f').onsubmit=(ev)=>{ev.preventDefault();
   if(document.getElementById('p').value==='right'){localStorage.setItem('app.admin.token','eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlX3ZhbHVlXzEyMw');root.innerHTML='<h1>Dashboard</h1><button>Users</button>';}
   else {localStorage.setItem('auth-storage', JSON.stringify({state:{token:null,error:'Invalid email or password'}})); loginView(true);}};}
loginView(false);
</script></body></html>"""


@pytest.fixture()
def page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=LOGIN_SPA))
    pg.goto("https://spa.qa/login")
    yield pg
    b.close()
    pw.stop()


def test_enter_signs_in_without_url_change(page):
    assert not _spa_signed_in(page, "/login")  # login form visible, no token
    page.fill("#e", "a@b.c")
    page.fill("#p", "right")
    page.press("#p", "Enter")
    page.wait_for_timeout(200)
    assert page.url.endswith("/login")         # the URL never changed...
    assert _spa_signed_in(page, "/login")      # ...but the user IS signed in


def test_failed_signin_inline_error_is_detected(page):
    page.fill("#e", "a@b.c")
    page.fill("#p", "wrong")
    page.press("#p", "Enter")
    page.wait_for_timeout(200)
    fb = page.evaluate(FEEDBACK_JS)
    assert any("Invalid email or password" in t for t in fb["alerts"] + fb["errors"]), fb
    assert not _spa_signed_in(page, "/login")


def test_root_login_failed_attempt_is_not_signed_in(page):
    """Sign-in screen served at '/' (Life Sadx): the path never mentions login, so the
    path alone must not count as signed in."""
    page.goto("https://spa.qa/")
    tok0 = set()
    page.fill("#e", "nobody@example.com")
    page.fill("#p", "wrong")
    page.press("#p", "Enter")
    page.wait_for_timeout(200)
    assert not _spa_signed_in(page, "/", tok0)
    page.fill("#e", "a@b.c")
    page.fill("#p", "right")
    page.press("#p", "Enter")
    page.wait_for_timeout(200)
    assert _spa_signed_in(page, "/", tok0)


def test_login_with_a_wrong_password_on_a_root_sign_in_screen_fails(tmp_path):
    """login() must not call a failed attempt 'signed in' just because the sign-in screen is at '/'."""
    from types import SimpleNamespace

    from playwright.sync_api import sync_playwright

    from qaplatform.explore.browser import login
    settings = SimpleNamespace(action_timeout_ms=3000, nav_timeout_ms=3000, settle_ms=150, headless=True)
    pw = sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        for password, expected in (("wrong", False), ("right", True)):
            ctx = b.new_context()
            ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=LOGIN_SPA))
            res = login(ctx, "https://spa.qa", "a@b.c", password, settings, tmp_path, login_path="/", save_state=False)
            assert res.success is expected, (password, res.error)
            ctx.close()
        b.close()
    finally:
        pw.stop()


def test_login_reports_a_failing_backend_as_a_server_error(tmp_path):
    from types import SimpleNamespace

    from playwright.sync_api import sync_playwright

    from qaplatform.explore.browser import login
    html = """<!doctype html><html><body><form id="f"><input type="email" id="e"><input type="password" id="p"><button type="submit">Sign In</button></form>
    <script>document.getElementById('f').onsubmit=async(ev)=>{ev.preventDefault(); await fetch('/api/admin/auth/login',{method:'POST',body:'{}'});};</script></body></html>"""
    settings = SimpleNamespace(action_timeout_ms=3000, nav_timeout_ms=3000, settle_ms=150, headless=True)
    pw = sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = b.new_context()
        ctx.route("**/api/admin/auth/login", lambda r: r.fulfill(status=500, content_type="application/json", body='{"error":"Something went wrong"}'))
        ctx.route("https://spa.qa/", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
        res = login(ctx, "https://spa.qa", "a@b.c", "x", settings, tmp_path, login_path="/", save_state=False)
        assert not res.success and res.server_error == 500 and "server error" in res.error
        b.close()
    finally:
        pw.stop()
