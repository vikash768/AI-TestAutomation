"""Web UI and API: submit a run, watch it, open the report, expose metrics."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qaplatform.app.assertions import validate_api_assertion, validate_ui_assertion
from qaplatform.app.uploads import ApkUploadTooLarge, save_apk_upload
from qaplatform.config import Settings
from qaplatform.db.models import Component, Finding, ReviewLabel, Run, RunEvent, Test, new_id
from qaplatform.db.session import init_db, session
from qaplatform.heartbeat import live_platforms
from qaplatform.report.legacy import refresh_saved_report, saved_report_needs_refresh
from qaplatform.report.metrics import build_registry
from qaplatform.secrets import encrypt_secrets


class RunArtifacts(StaticFiles):
    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        resolved, stat_result = super().lookup_path(path)
        if Path(resolved).name == "storage_state.json":
            return "", None  # Never serve browser session state left by old runs.
        return resolved, stat_result


settings = Settings.from_env()
init_db(settings.database_url)
settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI-TestAutomation", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
runs_dir = settings.artifacts_dir / "runs"
runs_dir.mkdir(exist_ok=True)
app.mount("/artifacts/runs", RunArtifacts(directory=str(runs_dir)), name="artifacts")
registry = build_registry(settings.database_url)
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
PLATFORMS = ("web", "android", "ios", "macos")
ANDROID_TEST_MODES = ("frontend", "backend", "full")


def db_session():
    db = session(settings.database_url)
    try:
        yield db
    finally:
        db.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(db_session)):
    runs = db.execute(select(Run).order_by(Run.created_at.desc()).limit(25)).scalars().all()
    return templates.TemplateResponse(request, "index.html", {"runs": runs, "grafana_url": GRAFANA_URL, "defaults": form_defaults()})


def form_defaults() -> dict:
    """Per-platform values the form starts with (from .env); switching platform swaps them in."""
    env = os.environ.get
    out: dict = {"platform": env("DEFAULT_PLATFORM", "web"), "web": {
        "repo_url": "", "live_url": env("TARGET_LIVE_URL", env("BASE_URL", "")),
        "email": env("VALID_USER_EMAIL", ""), "max_pages": settings.max_pages}}
    for platform, prefix in (("android", "ANDROID"), ("ios", "IOS"), ("macos", "DESKTOP")):
        out[platform] = {"repo_url": "", "app_path": "" if platform == "android" else env(f"{prefix}_APP_PATH", ""),
                         "email": env(f"{prefix}_USER_EMAIL", ""), "max_pages": settings.max_screens}
    out["load_test"] = {"enabled": env("LOAD_TEST_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"), "base_url": env("LOAD_TEST_BASE_URL", "")}
    return out


def parse_sources(raw: str) -> list[str]:
    """One codebase per line (or comma / whitespace separated): git URLs or local paths; blank lines and duplicates dropped."""
    out: list[str] = []
    for part in re.split(r"[\r\n]+|,\s*(?=(?:https?://|git@|/|~|\.))", raw or ""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _parse_http_url(source: str) -> SplitResult | None:
    """Accept HTTP URLs without userinfo, query parameters or fragments."""
    if re.search(r"[\s\\?#\x00-\x1f\x7f]", source):
        return None
    try:
        url = urlsplit(source)
        port = url.port
        if url.scheme in ("http", "https") and url.hostname and url.username is None and port != 0:
            return url
    except ValueError:
        pass
    return None


@app.post("/runs")
def create_run(
    project: str = Form(""), repo_url: str = Form(""), live_url: str = Form(""), email: str = Form(""), password: str = Form(""),
    github_token: str = Form(""), max_pages: int = Form(60), llm_intent: bool = Form(False), phase2: bool = Form(False), branch: str = Form(""),
    platform: str = Form("web"), app_path: str = Form(""), clean_start: bool = Form(False),
    journeys: bool = Form(False), remote_qa_data: bool = Form(False), load_test: bool = Form(False), load_test_base_url: str = Form(""),
    apk_file: UploadFile | None = File(None),
    test_mode: str = Form("frontend"),
    ui_path: str = Form(""), ui_expected_text: str = Form(""),
    api_base_url: str = Form(""), api_path: str = Form(""), api_expected_status: str = Form(""),
    api_json_field: str = Form(""), api_expected_value: str = Form(""), api_token: str = Form(""),
    db: Session = Depends(db_session),
):
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform must be one of {', '.join(PLATFORMS)}")
    project = project.strip()
    if platform == "android" and not project:
        raise HTTPException(400, "Project name is required for Android testing")
    if len(project) > 80:
        raise HTTPException(400, "Project name must be 80 characters or fewer")
    if platform == "android" and test_mode not in ANDROID_TEST_MODES:
        raise HTTPException(400, "Android test mode must be frontend, backend, or full")
    target = live_url.strip() if platform == "web" else app_path.strip()
    upload = apk_file if apk_file and apk_file.filename else None
    backend_only = platform == "android" and test_mode == "backend"
    if backend_only and (target or upload is not None):
        raise HTTPException(400, "Choose Frontend or Full testing to submit an APK or installed app ID")
    if upload is not None:
        if platform != "android":
            raise HTTPException(400, "APK uploads are supported for Android runs")
        if target:
            raise HTTPException(400, "Provide an APK file or a build path or installed app ID")
        target = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if Path(target).suffix.lower() != ".apk" or any(ord(c) < 32 or ord(c) == 127 for c in target):
            raise HTTPException(400, "Choose a file with the .apk extension")
    sources = parse_sources(repo_url)
    assertions = {}
    run_secrets = {}
    if platform == "android":
        if not backend_only and not target:
            raise HTTPException(400, "Provide an APK file, a build path, or an installed app ID")
        max_pages = settings.max_screens
        llm_intent = journeys = remote_qa_data = False
        if test_mode == "frontend":
            sources = []
            load_test = False
            branch = github_token = load_test_base_url = ""
        else:
            repo = _parse_http_url(sources[0]) if len(sources) == 1 else None
            if repo is None or repo.scheme != "https" or not repo.path.strip("/"):
                raise HTTPException(400, "Provide one HTTPS backend Git URL without credentials, query parameters, or fragments")
            sources = [repo.geturl()]
            github_token = github_token.strip()
            if not github_token:
                raise HTTPException(400, "A Git token is required for Backend and Full testing")
            base_url = api_base_url.strip() or load_test_base_url.strip()
            if load_test:
                base = _parse_http_url(base_url)
                if base is None:
                    raise HTTPException(400, "Provide an HTTP or HTTPS backend base URL without credentials, query parameters, or fragments to enable load testing")
                load_test_base_url = base.geturl()
            else:
                load_test_base_url = ""
            try:
                api_assertion = validate_api_assertion(base_url, api_path, api_expected_status, api_json_field, api_expected_value, api_token)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if api_assertion:
                assertions["api"] = api_assertion
                run_secrets["api_token"] = api_token.strip()
        if not backend_only:
            try:
                ui_assertion = validate_ui_assertion(ui_path, ui_expected_text)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if ui_assertion:
                assertions["ui"] = ui_assertion
        if backend_only:
            target = "Backend source review"
            email = password = ""
            phase2 = clean_start = False
    else:
        if not sources or not target:
            raise HTTPException(400, "repo_url and live_url are required" if platform == "web" else "repo_url and app_path are required")
        if len(sources) > (1 if platform == "web" else 2):
            raise HTTPException(400, "web runs take one codebase" if platform == "web" else "give at most two codebases: the app first, then its backend")
    options = {"max_pages": max_pages, "llm_intent": llm_intent, "phase2": phase2, "branch": branch.strip(), "clean_start": clean_start}
    if platform != "web":
        source_roles = ("backend",) if platform == "android" else ("frontend", "backend")
        options["codebase_sources"] = [{"role": role, "source": src} for role, src in zip(source_roles, sources)]
        options.update({"journeys": journeys, "remote_qa_data": remote_qa_data, "load_test": load_test, "load_test_base_url": load_test_base_url.strip()})
    if platform == "android":
        options["test_mode"] = test_mode
        if assertions:
            options["assertions"] = assertions
    if upload is not None:
        options["uploaded_apk"] = True
    run_secrets.update(email=email, password=password, github_token=github_token)
    run = Run(id=new_id(), project=project or "default", platform=platform, repo_url=sources[0] if sources else "", live_url=target, status="queued", stage="queued", options=options,
              secrets=encrypt_secrets(settings.secret_key, run_secrets))
    uploaded_path = None
    if upload is not None:
        try:
            uploaded_path = save_apk_upload(settings.artifacts_dir, run.id, upload.file)
        except ApkUploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        db.add(run)
        db.commit()
    except Exception:
        if uploaded_path is not None:
            uploaded_path.unlink(missing_ok=True)
        raise
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(run_id: str, request: Request, db: Session = Depends(db_session)):
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404)
    events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id.desc()).limit(60)).scalars().all()
    findings = db.execute(select(Finding).where(Finding.run_id == run_id).order_by(Finding.severity, Finding.route)).scalars().all()
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: (0 if f.status == "reported" else 1, sev_order.get(f.severity, 9)))
    report_exists = (settings.artifacts_dir / "runs" / run_id / "report.html").exists()
    return templates.TemplateResponse(request, "run.html", {"run": run, "events": list(reversed(events)), "findings": findings, "grafana_url": GRAFANA_URL, "report_exists": report_exists,
                                                             "waiting_for_worker": _waiting_for_worker(run)})


@app.get("/runs/{run_id}/report", response_class=HTMLResponse)
def run_report(run_id: str):
    path = settings.artifacts_dir / "runs" / run_id / "report.html"
    if not path.is_file():
        raise HTTPException(404, "report not ready")
    try:
        with path.open(encoding="utf-8") as report:
            head = report.read(512)
            if saved_report_needs_refresh(head):
                upgraded = refresh_saved_report(head + report.read(), run_id)
                if upgraded is not None:
                    return HTMLResponse(upgraded)
    except UnicodeDecodeError:
        pass
    return FileResponse(str(path), media_type="text/html")


@app.get("/api/runs")
def api_runs(db: Session = Depends(db_session)):
    runs = db.execute(select(Run).order_by(Run.created_at.desc()).limit(50)).scalars().all()
    return [_run_json(r) for r in runs]


def _waiting_for_worker(run: Run) -> str:
    """For a queued run nobody can claim: the command that starts a worker for its platform; else ''."""
    if run.status != "queued" or (run.platform or "web") in live_platforms(settings.artifacts_dir):
        return ""
    return "make up" if (run.platform or "web") == "web" else "make app-worker-start"


@app.get("/api/runs/{run_id}")
def api_run(run_id: str, db: Session = Depends(db_session)):
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404)
    counts = dict(db.execute(select(Finding.severity, func.count()).where(Finding.run_id == run_id, Finding.status == "reported").group_by(Finding.severity)).all())
    comps = dict(db.execute(select(Component.status, func.count()).where(Component.run_id == run_id).group_by(Component.status)).all())
    tests = dict(db.execute(select(Test.status, func.count()).where(Test.run_id == run_id).group_by(Test.status)).all())
    return {**_run_json(run), "findings_by_severity": counts, "components_by_status": comps, "tests_by_status": tests, "waiting_for_worker": _waiting_for_worker(run)}


@app.get("/api/runs/{run_id}/events")
def api_events(run_id: str, db: Session = Depends(db_session)):
    events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id).limit(500)).scalars().all()
    return [{"ts": e.ts.isoformat(), "level": e.level, "stage": e.stage, "message": e.message} for e in events]


@app.post("/api/findings/{finding_id}/label")
def label_finding(finding_id: int, verdict: str = Form(...), note: str = Form(""), reviewer: str = Form(""), db: Session = Depends(db_session)):
    if verdict not in ("true_positive", "false_positive"):
        raise HTTPException(400, "verdict must be true_positive or false_positive")
    f = db.get(Finding, finding_id)
    if f is None:
        raise HTTPException(404)
    db.add(ReviewLabel(finding_id=finding_id, verdict=verdict, note=note, reviewer=reviewer))
    f.status = "confirmed" if verdict == "true_positive" else "rejected"
    db.commit()
    return RedirectResponse(url=f"/runs/{f.run_id}", status_code=303)


def _run_json(r: Run) -> dict:
    return {"id": r.id, "project": r.project, "platform": r.platform, "test_mode": (r.options or {}).get("test_mode"), "repo_url": r.repo_url, "live_url": r.live_url, "status": r.status, "stage": r.stage, "progress": r.progress, "message": r.message,
            "error": r.error, "stats": r.stats, "created_at": r.created_at.isoformat() if r.created_at else None, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "commit_sha": r.commit_sha, "framework": r.framework}
