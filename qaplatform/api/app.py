"""Web UI and API: submit a run, watch it, open the report, expose metrics."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qaplatform.config import Settings
from qaplatform.db.models import Component, Finding, ReviewLabel, Run, RunEvent, Test
from qaplatform.db.session import init_db, session
from qaplatform.report.metrics import build_registry
from qaplatform.secrets import encrypt_secrets

settings = Settings.from_env()
init_db(settings.database_url)
settings.artifacts_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI-TestAutomation", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/artifacts", StaticFiles(directory=str(settings.artifacts_dir)), name="artifacts")
registry = build_registry(settings.database_url)
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")


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
    caps = {"users": settings.load_max_concurrency, "seconds": settings.load_max_seconds, "requests": settings.load_max_requests,
            "capacity_users": settings.capacity_max_users, "capacity_requests": settings.capacity_max_requests}
    return templates.TemplateResponse(request, "index.html", {"runs": runs, "grafana_url": GRAFANA_URL, "load_caps": caps, "defaults": {
        "repo_url": os.environ.get("TARGET_REPO_URL", ""), "live_url": os.environ.get("TARGET_LIVE_URL", ""),
        "email": os.environ.get("VALID_USER_EMAIL", ""), "project": os.environ.get("PROJECT_NAME", "")}})


@app.post("/runs")
def create_run(
    project: str = Form("default"), repo_url: str = Form(""), live_url: str = Form(...), email: str = Form(""), password: str = Form(""),
    github_token: str = Form(""), max_pages: int = Form(60), llm_intent: bool = Form(False), phase2: bool = Form(False), load: bool = Form(False), unit: bool = Form(False), provision: bool = Form(False), branch: str = Form(""),
    load_authorized: bool = Form(False), load_users: str = Form(""), load_seconds: str = Form(""), load_p95_ms: str = Form(""),
    load_mode: str = Form("ramp"), load_max_users: str = Form(""), load_step_seconds: str = Form(""), load_expected_users: str = Form(""),
    tier: str = Form("full"), owner_attested: bool = Form(False),
    db: Session = Depends(db_session),
):
    import re as _re
    tier = tier if tier in ("live", "backend", "full") else "full"
    repos = [u.strip() for u in _re.split(r"[\s,]+", repo_url.strip()) if u.strip()]
    if not live_url.strip():
        raise HTTPException(400, "a live URL is required")
    if tier == "live":
        # Tier 1: live URL only, read-only, no source code; requires the authorisation attestation.
        if not owner_attested:
            raise HTTPException(400, "tick the box confirming you own or are authorised to test this site")
        repos = []
    elif not repos:
        raise HTTPException(400, "the backend and full tiers need at least one GitHub repo URL")
    try:
        load_opts = _load_options(load, load_authorized, load_users, load_seconds, load_p95_ms, load_mode, load_max_users, load_step_seconds, load_expected_users)
    except ValueError as e:
        raise HTTPException(400, str(e))
    run = Run(project=project.strip() or "default", repo_url=repos[0] if repos else "", live_url=live_url.strip(), status="queued", stage="queued",
              options={"tier": tier, "owner_attested": owner_attested, "repos": repos, "max_pages": max_pages, "llm_intent": llm_intent, "phase2": phase2, **load_opts, "unit": unit, "provision": provision, "branch": branch.strip()},
              secrets=encrypt_secrets(settings.secret_key, {"email": email, "password": password, "github_token": github_token}))
    db.add(run)
    db.commit()
    return RedirectResponse(url=f"/runs/{run.id}", status_code=303)


LOAD_RANGES = {"load_users": (1, 50, 10), "load_seconds": (10, 120, 60), "load_p95_ms": (200, 30000, 2000),
               "load_max_users": (1, 500, 50), "load_step_seconds": (5, 60, 15), "load_expected_users": (0, 500, 0)}


def _load_options(load: bool, authorized: bool, users="", seconds="", p95_ms="", mode="ramp", max_users="", step_seconds="", expected_users="") -> dict:
    """Form values -> run.options load keys. Numbers are clamped rather than rejected (the worker caps again via LOAD_MAX_*);
    a blank or non-numeric field falls back to its default. Load without the authorisation box is refused outright."""
    if load and not authorized:
        raise ValueError("Load testing needs the 'I am authorised to load-test this site' box ticked — untick Load test or confirm authorisation.")
    out = {"load": bool(load), "load_authorized": bool(load and authorized), "load_mode": "capacity" if mode == "capacity" else "ramp"}
    for key, raw in (("load_users", users), ("load_seconds", seconds), ("load_p95_ms", p95_ms), ("load_max_users", max_users),
                     ("load_step_seconds", step_seconds), ("load_expected_users", expected_users)):
        lo, hi, default = LOAD_RANGES[key]
        try:
            v = int(float(str(raw).strip())) if str(raw).strip() else default
        except (ValueError, OverflowError):  # "abc", "nan", "inf"
            v = default
        out[key] = max(lo, min(hi, v))
    return out


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
    return templates.TemplateResponse(request, "run.html", {"run": run, "events": list(reversed(events)), "findings": findings, "grafana_url": GRAFANA_URL, "report_exists": report_exists})


@app.get("/runs/{run_id}/report")
def run_report(run_id: str):
    path = settings.artifacts_dir / "runs" / run_id / "report.html"
    if not path.exists():
        raise HTTPException(404, "report not ready")
    return FileResponse(str(path), media_type="text/html")


@app.get("/runs/{run_id}/{asset:path}")
def run_asset(run_id: str, asset: str):
    """Files the report links relatively (screenshots/…), served only from inside that run's folder."""
    run_dir = (settings.artifacts_dir / "runs" / run_id).resolve()
    path = (run_dir / asset).resolve()
    if not path.is_relative_to(run_dir) or not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".html", ".json"):
        raise HTTPException(404)
    if path.name in ("storage_state.json",) or "state" in path.stem.lower():
        raise HTTPException(404)  # never serve session material
    return FileResponse(str(path))


@app.get("/api/runs")
def api_runs(db: Session = Depends(db_session)):
    runs = db.execute(select(Run).order_by(Run.created_at.desc()).limit(50)).scalars().all()
    return [_run_json(r) for r in runs]


@app.get("/api/runs/{run_id}")
def api_run(run_id: str, db: Session = Depends(db_session)):
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404)
    counts = dict(db.execute(select(Finding.severity, func.count()).where(Finding.run_id == run_id, Finding.status == "reported").group_by(Finding.severity)).all())
    comps = dict(db.execute(select(Component.status, func.count()).where(Component.run_id == run_id).group_by(Component.status)).all())
    tests = dict(db.execute(select(Test.status, func.count()).where(Test.run_id == run_id).group_by(Test.status)).all())
    return {**_run_json(run), "findings_by_severity": counts, "components_by_status": comps, "tests_by_status": tests}


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
    return {"id": r.id, "project": r.project, "repo_url": r.repo_url, "live_url": r.live_url, "status": r.status, "stage": r.stage, "progress": r.progress, "message": r.message,
            "error": r.error, "stats": r.stats, "created_at": r.created_at.isoformat() if r.created_at else None, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "commit_sha": r.commit_sha, "framework": r.framework}
