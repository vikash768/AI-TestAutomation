"""The write guard is the safety net for every Phase 2 module; prove it blocks writes and allows reads."""
import pytest

from qaplatform.execute.guard import NetworkGuard, matches_any, path_regex
from tests.fixture.server import ORIGIN, FixtureServer


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    yield b
    b.close()
    pw.stop()


def test_guard_blocks_writes_allows_reads_and_refresh(browser):
    server = FixtureServer()
    ctx = browser.new_context()
    server.install(ctx)
    guard = NetworkGuard(ctx).install()
    page = ctx.new_page()
    page.goto(ORIGIN + "/login")
    statuses = page.evaluate("""async () => {
      const out = {};
      for (const [m, p] of [['GET', '/api/items'], ['POST', '/api/items'], ['DELETE', '/api/items/1'], ['PATCH', '/api/me'], ['POST', '/api/users/refresh']]) {
        try { const r = await fetch(p, { method: m, body: m === 'GET' ? undefined : '{"name":"x","password":"secret"}', headers: {'Content-Type': 'application/json'} }); out[m + ' ' + p] = r.status; }
        catch (e) { out[m + ' ' + p] = 'blocked'; }
      }
      out.beacon = navigator.sendBeacon('/api/beacon', 'x');
      return out;
    }""")
    assert statuses["GET /api/items"] == 200
    assert statuses["POST /api/items"] == "blocked" and statuses["DELETE /api/items/1"] == "blocked" and statuses["PATCH /api/me"] == "blocked"
    assert statuses["POST /api/users/refresh"] == 200  # session plumbing passes
    assert statuses["beacon"] is False
    sent = {(m, p) for m, p in server.calls}
    assert ("POST", "/api/items") not in sent and ("DELETE", "/api/items/1") not in sent and ("PATCH", "/api/me") not in sent
    assert {(w.method, w.path) for w in guard.intercepted} >= {("POST", "/api/items"), ("DELETE", "/api/items/1"), ("PATCH", "/api/me")}
    assert all("secret" not in str(w.body_keys) for w in guard.intercepted) and ["name", "password"] in [w.body_keys for w in guard.intercepted]
    with guard.suspended():
        s = page.evaluate("async () => (await fetch('/api/items', {method: 'POST'})).status")
    assert s == 200
    ctx.close()


def test_fault_injection_modes(browser):
    server = FixtureServer()
    ctx = browser.new_context()
    server.install(ctx)
    guard = NetworkGuard(ctx).install()
    page = ctx.new_page()
    page.goto(ORIGIN + "/login")
    guard.inject_server_error(502)
    assert page.evaluate("async () => (await fetch('/api/items')).status") == 502
    guard.inject_offline()
    assert page.evaluate("async () => { try { await fetch('/api/items'); return 'ok'; } catch (e) { return 'failed'; } }") == "failed"
    guard.inject_hold()
    page.evaluate("() => { window.__r = 'pending'; fetch('/api/items').then(r => window.__r = r.status); }")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__r") == "pending" and len(guard.held) == 1
    guard.clear_injection()
    page.wait_for_function("() => window.__r === 200", timeout=3000)
    ctx.close()


def test_path_regex():
    assert path_regex("/api/orgs/${orgId}/members").search("/api/orgs/12/members")
    assert path_regex("/api/orgs/${orgId}/members${toQueryString(f)}").search("/api/orgs/12/members")
    assert not path_regex("/api/orgs/${orgId}/members").search("/api/orgs/12/members/3")
    assert path_regex("/api/orgs/${orgId}/members/${memberId}").search("/api/orgs/1/members/37")
    assert not path_regex("/api/orgs/${orgId}/members/${memberId}").search("/api/orgs/1/members")
    assert matches_any("/svc/api/orgs/1/settings", ["/api/orgs/${orgId}", "/api/orgs/${orgId}/settings"]) == "/api/orgs/${orgId}/settings"
