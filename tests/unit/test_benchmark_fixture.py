"""Seeded-bug benchmark: the full execute stage against a fixture app with known bugs.

Recall: every planted bug must be reported. Precision: no finding may appear
outside the planted bugs and the few genuine side effects listed below.
Safety: no write other than sign-in / logout may reach the fixture's server.
"""
import re
from pathlib import Path

import pytest

from qaplatform.config import Settings
from qaplatform.explore import browser as browser_mod
from qaplatform.explore.crawler import explore
from qaplatform.ingest.intent import _api_index, build_intent
from qaplatform.ingest.js_parser import ApiCall, CodeElement, CodeInventory, CodeRoute, StorageFact, TimerFact
from qaplatform.pipeline import build_components, coverage_after, execute_all
from qaplatform.reconcile.matcher import reconcile
from tests.fixture.server import EMAIL, ORIGIN, PASSWORD, FixtureServer

SEEDED = {
    "blank page on API failure": r"^/items goes blank when its data request fails$",
    "noValidate form skips required": r"^'Save note' on /items accepts Title left empty$",
    "save error swallowed": r"^'Save note' on /items gives no feedback when saving fails$",
    "auto-refresh off keeps polling": r"^Turning off 'Auto-refresh' on /live does not stop polling$",
    "interval leaks after navigation": r"^/live keeps polling after the user leaves the page$",
    "logout needs the server": r"^Logout keeps the session when the logout request fails$",
    "destructive without confirmation": r"^'Archive now' on /home runs without a confirmation step$",
    "drawer not a dialog": r"is not exposed as a dialog$",
    "focus not moved into drawer": r"^Focus stays behind the panel",
    "drawer ignores Escape": r"does not close with Escape$",
}
# Real consequences of the fixture's code that the oracles rightly flag, but were not planted on purpose.
ALSO_ACCEPTABLE = [r"filled with spaces only$", r"^Unsaved changes on /(settings|profile) are discarded without warning$",
                   r"^/live does not tell the user its data failed to load$"]
MUST_NEED_REVIEW = [r"^'Archive now'", r"^Unsaved changes", r"^/live does not tell"]


def _inventory() -> CodeInventory:
    inv = CodeInventory(framework="react")
    inv.routes = [CodeRoute("/login", "Login", False), CodeRoute("/home", "Home", True, ["Shell"]), CodeRoute("/items", "Items", True, ["Shell"]),
                  CodeRoute("/live", "Live", True, ["Shell"]), CodeRoute("/settings", "Settings", True, ["Shell"]), CodeRoute("/profile", "Profile", True, ["Shell"])]
    inv.page_files = {"/home": "src/Home.jsx", "/items": "src/Items.jsx", "/live": "src/Live.jsx", "/settings": "src/Settings.jsx", "/profile": "src/Profile.jsx", "/login": "src/Login.jsx"}
    inv.api_calls = [ApiCall("src/api.js", "updateItem", "PATCH", "/api/items/${id}"), ApiCall("src/api.js", "deleteItem", "DELETE", "/api/items/${id}"),
                     ApiCall("src/api.js", "createItem", "POST", "/api/items"), ApiCall("src/api.js", "createNote", "POST", "/api/notes"),
                     ApiCall("src/api.js", "logout", "POST", "/api/logout"), ApiCall("src/api.js", "saveSettings", "PATCH", "/api/settings")]
    inv.timers = [TimerFact("src/Live.jsx", 30, "setInterval", 15000, "Live", ["poll"])]
    inv.storage = [StorageFact("src/auth.js", 3, "setItem", "fx.refresh")]
    E = []

    def el(route, kind, label, file, line, **kw):
        e = CodeElement(file=file, line=line, tag="button", kind=kind, label=label, route=route, **kw)
        e.key = f"{route}|{kind}|{label}|{file}:{line}"
        E.append(e)

    for i, (lab, href) in enumerate([("Home", "/home"), ("Items", "/items"), ("Live", "/live"), ("Settings", "/settings"), ("Profile", "/profile")]):
        el("(layout)", "link", lab, "src/Shell.jsx", 10 + i, href=href, navigates_to=[href])
    el("(layout)", "button", "Log out", "src/Shell.jsx", 20, calls=["logout"], handler="handleLogout")
    el("/home", "button", "Actions ▾", "src/Home.jsx", 5, opens=["toggle"], handler="inline")
    el("/home", "menuitem", "Edit", "src/Home.jsx", 6, opens=["drawer"], deferred_calls=["api.updateItem"], conditional=True)
    el("/home", "menuitem", "Delete", "src/Home.jsx", 7, opens=["confirm"], deferred_calls=["api.deleteItem"], conditional=True)
    el("/home", "menuitem", "Archive now", "src/Home.jsx", 8, calls=["api.deleteItem"], conditional=True)
    el("/items", "button", "+ Add item", "src/Items.jsx", 5, opens=["drawer"], deferred_calls=["api.createItem"])
    el("/items", "button", "Quick note", "src/Items.jsx", 6, opens=["drawer"], deferred_calls=["api.createNote"])
    el("/live", "checkbox", "Auto-refresh", "src/Live.jsx", 12)
    el("/settings", "button", "Retry", "src/Settings.jsx", 40, conditional=True)
    el("/settings", "button", "Save settings", "src/Settings.jsx", 50, attrs={"type": "submit"}, in_form=True)
    inv.elements = E
    return inv


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("bench")
    server = FixtureServer()
    browser_mod.CONTEXT_HOOKS.append(server.install)
    try:
        settings = Settings(settle_ms=500, action_timeout_ms=3000, nav_timeout_ms=10000, max_pages=10,
                            viewports=[("mobile", 360, 780), ("desktop", 1440, 900)], artifacts_dir=run_dir)
        inv = _inventory()
        api = _api_index(inv)
        intents = {e.key: build_intent(e, inv, api) for e in inv.elements}
        events = lambda *a, **k: None  # noqa: E731
        ex = explore(settings, ORIGIN, inv.routes, EMAIL, PASSWORD, run_dir, events)
        assert ex.login and ex.login.success, ex.warnings
        protected = [r.path for r in inv.routes if r.protected]
        rec = reconcile(inv.elements, ex.pages, protected, ex.origin)
        components = build_components(rec, intents)
        tests, findings, stats = execute_all(settings, ORIGIN, {"email": EMAIL, "password": PASSWORD}, run_dir, inv, ex, components, protected, {}, events, tag="bench")
        yield {"tests": tests, "findings": findings, "stats": stats, "components": components, "server": server}
    finally:
        browser_mod.CONTEXT_HOOKS.remove(server.install)


def test_no_module_crashed(outcome):
    errors = {k: v for k, v in outcome["stats"]["modules"].items() if "error" in v}
    assert not errors, errors


def test_recall_every_seeded_bug_is_reported(outcome):
    titles = [f.title for f in outcome["findings"]]
    missing = [name for name, rx in SEEDED.items() if not any(re.search(rx, t) for t in titles)]
    assert not missing, f"missed: {missing}\nfound: {sorted(set(titles))}"


def test_precision_nothing_else_is_reported(outcome):
    allowed = list(SEEDED.values()) + ALSO_ACCEPTABLE
    extra = sorted({f.title for f in outcome["findings"] if not any(re.search(rx, f.title) for rx in allowed)})
    assert not extra, f"unexpected findings: {extra}"


def test_heuristic_and_static_findings_wait_for_review(outcome):
    for f in outcome["findings"]:
        if any(re.search(rx, f.title) for rx in MUST_NEED_REVIEW):
            assert f.status == "needs_review", (f.title, f.status)


def test_no_write_reached_the_server(outcome):
    writes = {(m, p) for m, p in outcome["server"].calls if m not in ("GET", "HEAD", "OPTIONS")}
    assert writes <= {("POST", "/api/login"), ("POST", "/api/logout")}, writes


def test_coverage_and_correct_behaviour_passes(outcome):
    tests = outcome["tests"]
    by_name = {t.name: t for t in tests}
    passes = [
        r"^'Delete' on /home asks for confirmation and Cancel changes nothing$",
        r"^'Retry' on /settings recovers once the API is back$",
        r"^Reloading /items keeps the user there, signed in$",
        r"^Valid 'Save settings' submits a write",
        r"^'Save item' tells the user when saving fails",
        r"^/live refreshes every 15s as coded$",
        r"^Sign-in errors do not reveal which accounts exist$",
        r"^Back after logout does not show protected pages$",
    ]
    for rx in passes:
        hits = [t for n, t in by_name.items() if re.search(rx, n)]
        assert hits, f"no test matching {rx}"
        assert all(t.status == "pass" for t in hits), [(t.name, t.status, t.observed) for t in hits]
    cov = coverage_after(outcome["components"])
    assert cov["coverage_interaction"] >= 0.9, cov
    edit = next(c for c in outcome["components"] if c.label == "Edit" and c.route == "/home")
    assert edit.source == "both" and edit.reveal and edit.status == "pass"
