from pathlib import Path

import pytest

from qaplatform.config import Settings
from qaplatform.db.models import Run
from qaplatform.db.session import init_db, session
from qaplatform.explore.crawler import ExploreResult
from qaplatform.ingest.repo import RepoInfo
from qaplatform.ingest.workspace import repository_workspace
from qaplatform.pipeline import run_pipeline


def test_private_workspaces_are_independent_and_cleaned(tmp_path):
    artifacts = tmp_path / "artifacts"
    with repository_workspace(artifacts) as active:
        with pytest.raises(RuntimeError, match="source review failed"):
            with repository_workspace(artifacts) as failed:
                assert failed != active
                assert not failed.is_relative_to(artifacts)
                (failed / "source.txt").write_text("fixture")
                raise RuntimeError("source review failed")
        assert not failed.exists()
        assert active.is_dir()
    assert not active.exists()


@pytest.mark.parametrize("explore_fails", [False, True])
def test_web_run_removes_private_checkout_and_browser_state(tmp_path, monkeypatch, explore_fails):
    from qaplatform import pipeline

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", artifacts_dir=tmp_path / "artifacts")
    init_db(settings.database_url)
    with session(settings.database_url) as db:
        run = Run(project="private-workspace", repo_url="https://example.com/app.git", live_url="https://example.com")
        db.add(run)
        db.commit()
        run_id = run.id

    private_dirs: list[Path] = []

    def fetch_fixture(url, checkout, **kwargs):
        assert not checkout.is_relative_to(settings.artifacts_dir)
        checkout.mkdir()
        private_dirs.append(checkout.parent)
        return RepoInfo(path=checkout)

    def explore_fixture(settings, live_url, routes, email, password, run_dir, events, *, state_dir):
        assert not state_dir.is_relative_to(run_dir)
        storage = state_dir / "storage_state.json"
        storage.write_text("{}")
        if explore_fails:
            raise RuntimeError("fixture exploration failed")
        return ExploreResult(origin=live_url, storage_state_path=str(storage))

    def execute_fixture(settings, live_url, secrets, run_dir, inv, ex, components, protected_routes, options, events, tag):
        assert Path(ex.storage_state_path).is_file()
        return [], [], {}

    monkeypatch.setattr(pipeline, "fetch_repo", fetch_fixture)
    monkeypatch.setattr(pipeline, "explore", explore_fixture)
    monkeypatch.setattr(pipeline, "execute_all", execute_fixture)
    run_pipeline(run_id, settings)

    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        assert run.status == ("failed" if explore_fails else "completed")
        if explore_fails:
            assert "fixture exploration failed" in run.error
    if not explore_fails:
        report = (settings.artifacts_dir / "runs" / run_id / "report.html").read_text()
        assert "storage_state.json" not in report
    assert len(private_dirs) == 1 and not private_dirs[0].exists()
    assert not list(settings.artifacts_dir.rglob("storage_state.json"))
