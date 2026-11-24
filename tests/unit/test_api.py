import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    db = Path("/tmp/qaplatform_test.db")
    if db.exists():
        db.unlink()
    from qaplatform.api import app as app_module
    return TestClient(app_module.app)


def test_index_and_create_run(client):
    r = client.get("/")
    assert r.status_code == 200 and "Start run" in r.text
    r = client.post("/runs", data={"repo_url": "https://github.com/x/y.git", "live_url": "https://example.com", "email": "a@b.c", "password": "Plaintext-Probe-9f3c", "project": "demo", "owner_attested": "true"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    j = client.get(f"/api/runs/{run_id}").json()
    assert j["status"] == "queued" and j["project"] == "demo"
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200 and run_id in page.text
    assert "Plaintext-Probe-9f3c" not in page.text


def test_secrets_are_encrypted_at_rest(client):
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.secrets import decrypt_secrets
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.query(Run).first()
        assert run.secrets and "Plaintext-Probe-9f3c" not in run.secrets
        assert decrypt_secrets("test-key", run.secrets)["password"] == "Plaintext-Probe-9f3c"


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and "qa_runs_total" in r.text


def test_live_tier_runs_without_a_repo(client):
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    # Tier 1: live URL only, no repo, but needs the authorisation attestation.
    r = client.post("/runs", data={"tier": "live", "live_url": "https://example.com", "owner_attested": "true", "project": "live-demo"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.get(Run, run_id)
        assert run.options["tier"] == "live"
        assert run.options["repos"] == [] and run.repo_url == ""
        assert run.options["owner_attested"] is True


def test_live_tier_requires_the_attestation(client):
    r = client.post("/runs", data={"tier": "live", "live_url": "https://example.com"}, follow_redirects=False)
    assert r.status_code == 400 and "authorised" in r.text


def test_backend_and_full_tiers_still_require_a_repo(client):
    r = client.post("/runs", data={"tier": "full", "live_url": "https://example.com"}, follow_redirects=False)
    assert r.status_code == 400 and "repo" in r.text.lower()


def _live_enabled(has_creds):
    # mirror the Tier-1 gate in execute_all
    PHASE2 = {"reveal", "destructive", "forms", "auth", "state", "lifecycle", "faults"}
    enabled = set(PHASE2) | {"security"}
    drop = {"security", "provision", "load", "unit"}
    if not has_creds:
        drop |= {"forms", "auth"}
    return enabled - drop


def test_anonymous_live_tier_excludes_login_and_form_submission():
    enabled = _live_enabled(has_creds=False)
    assert "security" not in enabled and "forms" not in enabled and "auth" not in enabled
    assert {"reveal", "state", "lifecycle", "faults", "destructive"} <= enabled


def test_live_tier_with_credentials_runs_the_login_suite():
    # With credentials + attestation, Tier 1 runs auth (login security) and forms, but still no
    # backend probes / writes / load.
    enabled = _live_enabled(has_creds=True)
    assert "auth" in enabled and "forms" in enabled
    assert "security" not in enabled and "provision" not in enabled and "load" not in enabled


def test_finding_bucket_tiers():
    from qaplatform.execute.smoke import FindingRecord
    from qaplatform.report.html import finding_bucket
    def f(cat, title="x", sev="medium"):
        return FindingRecord(fingerprint=title, route="/", category=cat, severity=sev, title=title)
    assert finding_bucket(f("stability", "Server error during load of /")) == "problem"
    assert finding_bucket(f("ui-responsive", "Tap targets under 24px")) == "review"
    assert finding_bucket(f("accessibility", "Clipped text on /")) == "review"
    assert finding_bucket(f("ui-responsive", "Horizontal overflow on / at mobile")) == "problem"  # page scrolls sideways: visible
    assert finding_bucket(f("information", "Third-party resources on /")) == "info"
    assert finding_bucket(f("performance", "/ took 6000 ms to load")) == "info"


def test_live_quick_depth_disables_phase2(client):
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    r = client.post("/runs", data={"tier": "live", "live_url": "https://example.com", "owner_attested": "true", "depth": "quick"}, follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.get(Run, rid)
        assert run.options["depth"] == "quick" and run.options["phase2"] is False


def test_backend_tier_requires_attestation(client):
    r = client.post("/runs", data={"tier": "backend", "live_url": "https://example.com", "repo_url": "https://github.com/x/y.git"}, follow_redirects=False)
    assert r.status_code == 400 and "authorised" in r.text


def test_url_repo_corroboration(tmp_path):
    from qaplatform.pipeline import _repo_mentions_host
    (tmp_path / "README.md").write_text("Live at https://app.example.com")
    assert _repo_mentions_host([tmp_path], "app.example.com") is True
    assert _repo_mentions_host([tmp_path], "www.app.example.com") is True
    assert _repo_mentions_host([tmp_path], "victim.com") is False
