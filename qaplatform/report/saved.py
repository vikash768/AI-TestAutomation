"""Render screen results for previously saved device runs."""
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from qaplatform.db.models import Component, Finding, Page, Run, Test
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedPage
from qaplatform.report.html import NATIVE, render_report


def _screen_paths(run_dir: Path) -> dict[str, list[str]]:
    try:
        trace = json.loads((run_dir / "explore.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    paths = {}
    for screen in trace.get("screens", []) if isinstance(trace, dict) else []:
        route, path = screen.get("route"), screen.get("path")
        if isinstance(route, str) and isinstance(path, list):
            paths.setdefault(route, [label for label in path if isinstance(label, str) and label])
    return paths


def render_saved_device_report(db: Session, run_dir: Path, run_id: str) -> str | None:
    run = db.get(Run, run_id)
    if run is None or run.platform not in NATIVE:
        return None
    stats = {**(run.stats or {}), "platform": run.platform}
    if stats.get("test_mode") == "backend":
        return None
    paths = _screen_paths(run_dir)
    pages = [
        ObservedPage(route=p.route, url=p.url, title=p.title, status_code=p.status_code, load_ms=p.load_ms or 0,
                     screenshot=p.screenshot, viewports=p.viewports or {}, console_errors=p.console_errors or [],
                     failed_requests=p.failed_requests or [], state={"path": paths.get(p.route, [])})
        for p in db.scalars(select(Page).where(Page.run_id == run_id).order_by(Page.id))
    ]
    if not pages:
        return None
    components = []
    for c in db.scalars(select(Component).where(Component.run_id == run_id).order_by(Component.id)):
        attrs = c.attrs or {}
        components.append(ComponentRecord(
            key=str(c.id), route=c.route, kind=c.kind, label=c.label, source=c.source, code_file=c.code_file,
            code_line=c.code_line, selector=c.selector, name=attrs.get("name") or "", page_url=attrs.get("page_url"),
            intent=c.intent or {}, action_class=c.action_class, status=c.status, notes=c.notes, reveal=attrs.get("reveal") or [],
        ))
    findings = []
    evidence_by_test = defaultdict(list)
    for f in db.scalars(select(Finding).where(Finding.run_id == run_id).order_by(Finding.id)):
        evidence = f.evidence or []
        if f.test_id is not None:
            evidence_by_test[f.test_id].extend(item for item in evidence if item not in evidence_by_test[f.test_id])
        findings.append(FindingRecord(
            fingerprint=f.fingerprint, route=f.route, category=f.category, severity=f.severity, title=f.title,
            description=f.description, expected=f.expected, observed=f.observed, standard_ref=f.standard_ref,
            confidence=f.confidence, reproduced_n=f.reproduced_n, reproduced_m=f.reproduced_m, status=f.status,
            evidence=evidence, fix_in=f.fix_in, fix=f.fix, code_ref=f.code_ref, steps=f.steps or [],
        ))
    tests = [
        TestRecord(test_key=t.test_key, name=t.name, technique=t.technique, category=t.category, route=t.route,
                   expected=t.expected, observed=t.observed, status=t.status, duration_ms=t.duration_ms,
                   steps=t.steps or [], evidence=evidence_by_test[t.id],
                   component_key=str(t.component_id) if t.component_id is not None else None)
        for t in db.scalars(select(Test).where(Test.run_id == run_id).order_by(Test.id))
    ]
    meta = {
        "live_url": (stats.get("build") or {}).get("app_id") or run.live_url,
        "framework": run.framework, "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "repo_url": run.repo_url, "commit_sha": run.commit_sha, "sources": stats.get("sources") or [],
    }
    return render_report(run_dir, run_id, meta, stats, pages, components, tests, findings, archived=True)
