import os
from pathlib import Path
from tempfile import TemporaryDirectory

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

    data = {"repo_url": "https://github.com/x/y.git", "live_url": "https://example.com",
            "password": "fixture-password", "github_token": "fixture-git-token"}
    response = client.post("/runs", data=data, follow_redirects=False)
    assert response.status_code == 303
    run_id = response.headers["location"].rsplit("/", 1)[-1]
    with session(os.environ["DATABASE_URL"]) as db:
        run = db.get(Run, run_id)
        assert run.secrets and data["password"] not in run.secrets and data["github_token"] not in run.secrets
        secrets = decrypt_secrets("test-key", run.secrets)
        assert secrets["password"] == data["password"]
        assert secrets["github_token"] == data["github_token"]


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and "qa_runs_total" in r.text


def test_run_activity_shows_latest_events_after_original_api_limit(client):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Run, RunEvent
    from qaplatform.db.session import session

    with session(settings.database_url) as db:
        run = Run(repo_url="", live_url="com.example.app", platform="android", status="running", progress=60)
        db.add(run)
        db.flush()
        run_id = run.id
        db.add_all(RunEvent(run_id=run_id, stage="explore", message=f"Activity event {i:04d}") for i in range(530))
        db.commit()

    path = f"/api/runs/{run_id}/events"
    initial = client.get(path).json()
    recent = client.get(path, params={"limit": 60}).json()
    assert len(initial) == 500 and initial[-1]["message"] == "Activity event 0499"
    assert [event["message"] for event in recent] == [f"Activity event {i:04d}" for i in range(470, 530)]

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200 and 'aria-valuenow="60"' in page.text
    assert "Activity event 0469" not in page.text
    assert "Activity event 0470" in page.text and "Activity event 0529" in page.text


def test_completed_run_shows_saved_check_results_and_findings_first(client):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Finding, Run, Test
    from qaplatform.db.session import session

    with session(settings.database_url) as db:
        run = Run(repo_url="", live_url="com.example.app", platform="android", status="completed", progress=100)
        db.add(run)
        db.flush()
        run_id = run.id
        db.add_all(Test(run_id=run_id, test_key=status, name=f"Fixture {status}", status=status) for status in ("pass", "fail"))
        db.add(Finding(run_id=run_id, fingerprint="fixture", title="Fixture finding", severity="high", category="functional"))
        db.commit()

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200 and '<h1 id="run-title">Run results</h1>' in page.text
    assert '<strong>2</strong> checks recorded' in page.text
    assert 'class="result-pass" style="flex-grow:1"' in page.text
    assert 'class="result-fail" style="flex-grow:1"' in page.text
    assert page.text.index("Fixture finding") < page.text.index('id="activity-heading"')


def test_artifact_mount_only_serves_run_evidence(client):
    from qaplatform.api.app import settings

    legacy_dir = settings.artifacts_dir / "repos"
    legacy_dir.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=legacy_dir) as legacy, TemporaryDirectory(dir=settings.artifacts_dir / "runs") as run:
        checkout = Path(legacy) / "fixture.py"
        checkout.write_text("private checkout")
        storage = Path(run) / "storage_state.json"
        storage.write_text("{}")
        evidence = Path(run) / "report.html"
        evidence.write_text("<h1>Run evidence</h1>")

        assert client.get(f"/artifacts/repos/{Path(legacy).name}/{checkout.name}").status_code == 404
        assert client.get(f"/artifacts/runs/{Path(run).name}/{storage.name}").status_code == 404
        response = client.get(f"/artifacts/runs/{Path(run).name}/{evidence.name}")
        assert response.status_code == 200 and response.text == "<h1>Run evidence</h1>"
        assert checkout.is_file() and storage.is_file()


def test_saved_report_gets_new_layout_without_rewriting_archive(client, monkeypatch, tmp_path):
    from qaplatform.api.app import settings

    run_id = "fixture-layout"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)
    evidence = f'/artifacts/runs/{run_id}/screenshots/proof.png'
    report = run_dir / "report.html"
    original = f'''<!doctype html><html><head><meta charset="utf-8"><title>QA report {run_id}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;}}</style></head><body>
<h1>QA report · run {run_id} · Frontend testing</h1>
<p>android: com.fixture · react-native · 2026-11-26T03:28:33+00:00<br><small>backend: fixture</small></p>
<div class="stats"><div class="stat"><b>1</b>fail</div></div>
<h2>Testing outcomes</h2><table><tbody><tr><td>Frontend</td><td>fail</td><td>1 failed</td></tr></tbody></table>
<h2>Findings</h2><table><tbody><tr class='sev-high st-reported'>
<td>high</td><td>reported</td><td>functional</td><td>Home</td>
<td><b>Tap &lt;Save&gt;</b><br><small>Fixture finding</small><br><small><b>Fix in app code</b> · Use accessible text</small></td>
<td>Screen loads</td><td>No change</td><td>Standard reference</td><td>0.90 (2/2)</td>
<td><a href="{evidence}" target="_blank">screenshot device</a></td></tr></tbody></table>
<h2>Tests by technique</h2><table><tbody><tr><td>smoke</td><td>0</td><td>1</td><td>0</td><td>0</td><td>0</td></tr></tbody></table>
<h2 id="tests">Tests</h2><table><tbody><tr class='t-fail'><td>Test &quot;save&quot;</td><td>functional<br><small>smoke</small></td>
<td>Home</td><td><ol><li>Tap &lt;Save&gt;</li></ol></td><td>Screen loads</td><td>No change</td><td>fail</td><td>123 ms</td>
<td><a href="{evidence}" target="_blank">screenshot device</a></td><td><small>app.js:10</small></td><td><small></small></td><td><small></small></td>
</tr></tbody></table></body></html>'''
    report.write_text(original, encoding="utf-8")

    response = client.get(f"/runs/{run_id}/report")
    assert response.status_code == 200
    assert 'name="qa-report-layout" content="2"' in response.text
    assert 'data-severity="high"' in response.text and 'class="test-result"' in response.text
    assert all(value in response.text for value in ('com.fixture', 'Tap &lt;Save&gt;', 'Fixture finding', 'Use accessible text', 'app.js:10'))
    assert response.text.count(f'href="{evidence}"') == 2
    assert report.read_text(encoding="utf-8") == original

    report.write_text("<h1>Independently saved report</h1>", encoding="utf-8")
    assert client.get(f"/runs/{run_id}/report").text == "<h1>Independently saved report</h1>"


def test_saved_redesigned_report_gets_live_section_highlight(client, monkeypatch, tmp_path):
    from qaplatform.api.app import settings

    run_dir = tmp_path / "runs" / "saved-navigation"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)
    report = run_dir / "report.html"
    original = '''<!doctype html>
<html lang="en"><head><meta name="qa-report-layout" content="2">
<style>.jump-nav a:first-child, .jump-nav a:hover { color: var(--blue); background: #e6effd; }</style>
</head><body><nav class="jump-nav"><a href="#findings">Findings</a><a href="#screens">Screens</a></nav>
<section id="findings">Saved findings</section><section id="screens">Saved screens</section></body></html>'''
    report.write_text(original, encoding="utf-8")

    response = client.get("/runs/saved-navigation/report")
    assert response.status_code == 200
    assert '.jump-nav a[aria-current="location"]' in response.text
    assert ".jump-nav a:first-child" not in response.text
    assert "selectFromHash" in response.text and '<section id="screens">Saved screens</section>' in response.text
    assert report.read_text(encoding="utf-8") == original


def test_saved_device_report_uses_screen_data_and_keeps_original(client, monkeypatch, tmp_path):
    from qaplatform.api.app import settings
    from qaplatform.db.models import Component, Page, Run, Test
    from qaplatform.db.session import session
    from qaplatform.report.html import REPORT_LAYOUT_MARKER, REPORT_NAVIGATION_MARKER, SCREEN_RESULTS_MARKER

    with session(settings.database_url) as db:
        run = Run(platform="macos", repo_url="", live_url="com.example.app", status="completed", stats={"screens": 1})
        db.add(run)
        db.flush()
        run_id = run.id
        page_url = "app://com.example.app/Compose#example"
        db.add(Page(run_id=run_id, route="Compose", url=page_url, title="Compose", screenshot="screenshots/window.png"))
        component = Component(run_id=run_id, component_key="save", route="(layout)", label="Save draft", kind="button",
                              source="observed", attrs={"page_url": page_url})
        db.add(component)
        db.flush()
        db.add(Test(run_id=run_id, test_key="tap-save", name="Save draft", route="Compose", status="fail", component_id=component.id))
        db.commit()

    run_dir = tmp_path / "runs" / run_id
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "screenshots/window.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    report = run_dir / "report.html"
    original = f'<!doctype html>\n<html lang="en"><head>{REPORT_LAYOUT_MARKER}{REPORT_NAVIGATION_MARKER}</head><body>Old report</body></html>'
    report.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "artifacts_dir", tmp_path)

    response = client.get(f"/runs/{run_id}/report")
    assert response.status_code == 200 and SCREEN_RESULTS_MARKER in response.text
    assert 'Compose</h3>' in response.text and 'Save draft</th>' in response.text and '1 failed' in response.text
    assert f'<img src="/artifacts/runs/{run_id}/screenshots/window.png"' in response.text
    assert f'href="/artifacts/runs/{run_id}/report.html"' in response.text
    assert report.read_text(encoding="utf-8") == original
