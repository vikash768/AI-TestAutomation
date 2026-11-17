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
    r = client.post("/runs", data={"repo_url": "https://github.com/x/y.git", "live_url": "https://example.com", "email": "a@b.c", "password": "pw", "project": "demo"}, follow_redirects=False)
    assert r.status_code == 303
    run_id = r.headers["location"].rsplit("/", 1)[-1]
    j = client.get(f"/api/runs/{run_id}").json()
    assert j["status"] == "queued" and j["project"] == "demo"
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200 and run_id in page.text
    assert "pw" not in page.text


def test_secrets_are_encrypted_at_rest(client):
    from qaplatform.db.models import Run
    from qaplatform.db.session import session
    from qaplatform.secrets import decrypt_secrets
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.query(Run).first()
        assert run.secrets and "pw" not in run.secrets
        assert decrypt_secrets("test-key", run.secrets)["password"] == "pw"


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and "qa_runs_total" in r.text
