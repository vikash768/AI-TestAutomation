"""Provision module: create a marked demo record, delete it, never touch the real one."""
import pytest

from qaplatform.config import Settings
from qaplatform.execute.provision import run_provision
from qaplatform.explore import browser as browser_mod
from qaplatform.explore.crawler import explore
from qaplatform.ingest.js_parser import CodeRoute
from qaplatform.pipeline import build_components
from qaplatform.reconcile.matcher import reconcile
from tests.fixture.server import CRUD_ORIGIN, EMAIL, PASSWORD, CrudServer


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("prov")
    server = CrudServer()
    browser_mod.CONTEXT_HOOKS.append(server.install)
    try:
        s = Settings(settle_ms=400, action_timeout_ms=3000, nav_timeout_ms=10000, max_pages=6, viewports=[("desktop", 1440, 900)], artifacts_dir=run_dir)
        routes = []  # router-less
        ex = explore(s, CRUD_ORIGIN, routes, EMAIL, PASSWORD, run_dir, lambda *a, **k: None, routeless=True)
        assert ex.login and ex.login.success, ex.warnings
        rec = reconcile([], ex.pages, [], ex.origin)
        components = build_components(rec, {})
        # build a runner the way execute_all does
        from qaplatform.execute.smoke import SmokeRunner
        runner = SmokeRunner(s, CRUD_ORIGIN, ex.storage_state_path, run_dir, {"email": EMAIL, "password": PASSWORD}, lambda *a, **k: None)
        tests, findings, stats = run_provision(runner, components, ex.pages, [], "/login", lambda *a, **k: None, tag="unittest")
        yield {"tests": tests, "findings": findings, "stats": stats, "server": server}
    finally:
        browser_mod.CONTEXT_HOOKS.remove(server.install)


def test_demo_record_created_and_deleted(outcome):
    names = [t.name for t in outcome["tests"]]
    assert any(t.technique == "provision" and "Create a demo record" in t.name and t.status == "pass" for t in outcome["tests"]), [(t.name, t.status) for t in outcome["tests"]]
    assert any("deletes the demo record" in t.name and t.status == "pass" for t in outcome["tests"]), [(t.name, t.status) for t in outcome["tests"]]
    assert outcome["stats"]["created"] >= 1 and outcome["stats"]["cleaned"] >= 1


def test_only_the_demo_was_created_and_deleted(outcome):
    server = outcome["server"]
    created = [p.split("/")[-1] for m, p in server.calls if m == "POST" and p == "/api/members"]
    assert server.deleted, "nothing was deleted"
    assert all(u.startswith("qaprobe") for u in server.deleted), f"deleted a non-demo record: {server.deleted}"
    assert "owner_real" not in server.deleted
    assert "owner_real" in server.members, "the real record must survive"


def test_data_dependent_control_exercised_on_demo(outcome):
    # Disable/Enable toggled the demo member's status
    assert any(m == "POST" and p.endswith("/toggle") for m, p in outcome["server"].calls)
    toggled = [p for m, p in outcome["server"].calls if m == "POST" and p.endswith("/toggle")]
    assert all("qaprobe" in p for p in toggled), f"toggled a non-demo record: {toggled}"
