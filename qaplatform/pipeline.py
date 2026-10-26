"""The seven-stage run, from a queued Run row to a finished report."""
from __future__ import annotations

import json
import logging
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from qaplatform.config import Settings
from qaplatform.db.models import Run, RunEvent
from qaplatform.db.session import session
from qaplatform.execute.smoke import ComponentRecord, SmokeRunner, guard_tests, page_tests
from qaplatform.explore.crawler import explore
from qaplatform.explore.safety import DOWNLOAD_EXT, classify_action
from qaplatform.ingest.intent import apply_enrichment, build_intent, enrich_with_llm, _api_index
from qaplatform.ingest.js_parser import build_inventory
from qaplatform.ingest.repo import fetch_repo
from qaplatform.reconcile.matcher import INTERACTIVE_KINDS, normalize, reconcile
from qaplatform.report.html import write_report
from qaplatform.report.writer import persist
from qaplatform.secrets import decrypt_secrets

logger = logging.getLogger(__name__)


ROLE_KIND = {"link": "link", "textbox": "input", "combobox": "select", "checkbox": "checkbox", "radio": "radio", "switch": "switch", "tab": "tab", "menuitem": "menuitem"}


def build_components(rec, intents: dict) -> list[ComponentRecord]:
    """Code components (matched or not) plus live controls the code does not declare."""
    components: list[ComponentRecord] = []
    for m in rec.matches:
        if m.reason == "twin":
            continue
        c = m.code
        intent = intents.get(c.key, {})
        row = ComponentRecord(
            key=c.key, route=c.route, kind=c.kind, label=c.label or "", source="both" if m.observed else "code", code_file=c.file, code_line=c.line, handler=c.handler or "",
            attrs=dict(c.attrs), intent=intent, action_class=intent.get("action_class", "safe"), conditional=c.conditional or c.repeated, href=c.href,
        )
        if m.observed is not None and m.page is not None:
            o = m.observed
            row.selector, row.role, row.name = o.selector, o.role, o.name
            row.href = o.href or c.href
            if not c.label or "{" in c.label:
                row.label = o.name  # show the live name when the code label is a template
            if o.download or (o.href and DOWNLOAD_EXT.search(o.href)):
                row.action_class = "download"
                intent["action_class"] = "download"
            row.page_url, row.page_protected = m.page.url, bool(m.page.protected)
            row.visible, row.enabled, row.in_dialog = o.visible, o.enabled, o.in_dialog
            row.attrs["haspopup"] = o.haspopup
            row.notes = f"matched by {m.reason}"
        else:
            row.status = "not_observed"
            row.notes = "declared in code; not found on the live page" + (" (conditional or data-driven)" if (c.conditional or c.repeated) else "")
        components.append(row)
    seen_obs: dict[tuple, ComponentRecord] = {}
    for page, o in rec.unmatched_observed:
        sig = (page.route, o.role, normalize(o.name) or o.selector)
        if sig in seen_obs:
            prev = seen_obs[sig]
            prev.attrs["instances"] = int(prev.attrs.get("instances", 1)) + 1
            prev.notes = f"present on live page, not declared in code ({prev.attrs['instances']} instances)"
            continue
        intent = {"expected": ["Responds without errors"], "observed_only": True}
        cls = classify_action(o.name, {"href": o.href, "type": o.type, "download": True if o.download else None}, intent)
        seen_obs[sig] = ComponentRecord(
            key=f"obs|{page.route}|{o.role}|{o.name}|{o.selector}"[:400], route=page.route, kind=ROLE_KIND.get(o.role, "button"),
            label=o.name, source="observed", selector=o.selector, role=o.role, name=o.name, href=o.href, attrs={"type": o.type, "tag": o.tag, "path_hint": o.path_hint, "haspopup": o.haspopup},
            intent={**intent, "action_class": cls}, action_class=cls, page_url=page.url, page_protected=bool(page.protected), visible=o.visible, enabled=o.enabled,
            in_dialog=o.in_dialog, notes="present on live page, not declared in code",
        )
        components.append(seen_obs[sig])
    return components


def coverage_after(components: list[ComponentRecord]) -> dict:
    code = [c for c in components if c.source in ("code", "both")]
    found = [c for c in code if c.source == "both"]
    tested = [c for c in components if c.status in ("pass", "fail", "flaky", "equivalent")]
    exercisable = [c for c in components if c.source in ("both", "observed")]
    return {
        "coverage_interaction": round(len(found) / len(code), 4) if code else 0.0,
        "matched_interaction": len(found),
        "revealed_components": sum(1 for c in components if c.reveal),
        "tested_components": len(tested),
        "equivalent_components": sum(1 for c in components if c.status == "equivalent"),
        "tested_ratio": round(len(tested) / len(exercisable), 4) if exercisable else 0.0,
    }


PHASE2_MODULES = ("reveal", "destructive", "forms", "auth", "state", "lifecycle", "faults")


def execute_all(settings: Settings, live_url: str, secrets: dict, run_dir: Path, inv, ex, components: list[ComponentRecord], protected_routes: list[str],
                options: dict, events, tag: str = "run") -> tuple[list, list, dict]:
    """Phase 1 checks and smoke clicks, then the Phase 2 modules. A module that fails is recorded, not fatal."""
    from qaplatform.execute.destructive import run_destructive
    from qaplatform.execute.faults import run_faults
    from qaplatform.execute.forms import FormTester, auth_tests
    from qaplatform.execute.lifecycle import run_lifecycle
    from qaplatform.execute.reveal import Revealer
    from qaplatform.execute.smoke import TestRecord
    from qaplatform.execute.state import run_state

    tests, findings = guard_tests(ex.guards)
    t2, f2 = page_tests(ex.pages, settings)
    tests += t2
    findings += f2
    stats: dict = {"modules": {}}
    events("execute", f"{len(tests)} page-level checks done; starting smoke clicks", 60)
    if not (ex.storage_state_path or secrets.get("email")):
        return tests, findings, stats
    runner = SmokeRunner(settings, live_url, ex.storage_state_path, run_dir, secrets, events)
    t3, f3 = runner.run(components, ex.pages, protected_routes)
    tests += t3
    findings += f3
    if not options.get("phase2", True):
        return tests, findings, stats

    enabled = set(options.get("modules") or PHASE2_MODULES)
    code_by_key = {e.key: e for e in inv.elements}
    write_paths: dict[str, list[str]] = {}
    for c in components:
        for call in (c.intent or {}).get("api_calls", []) + (c.intent or {}).get("deferred_api_calls", []):
            if call.get("method") not in ("GET", "HEAD"):
                write_paths.setdefault(c.route, []).append(call["path"])
    for a in inv.api_calls:
        if a.method not in ("GET", "HEAD"):
            write_paths.setdefault("(layout)", []).append(a.path)
    storage_keys = sorted({s.key for s in inv.storage if s.op == "setItem"})
    login_path = urlparse(ex.login.login_url).path if ex.login and ex.login.login_url else "/login"

    plan = [
        ("reveal", lambda: Revealer(runner, events).run(components, code_by_key, protected_routes, ex.origin)),
        ("destructive", lambda: run_destructive(runner, components, events)),
        ("forms", lambda: FormTester(runner, events, write_paths, tag[:8]).run(ex.pages, components, protected_routes, login_path)),
        ("auth", lambda: auth_tests(settings, live_url, ex.login.login_url if ex.login else "", secrets.get("email", ""), secrets.get("password", ""), events, tag[:8])),
        ("state", lambda: run_state(runner, ex.pages, components, protected_routes, storage_keys, events)),
        ("lifecycle", lambda: run_lifecycle(runner, inv, ex.pages, components, events)),
        ("faults", lambda: run_faults(runner, ex.pages, components, inv.elements, events)),
    ]
    for name, fn in plan:
        if name not in enabled:
            continue
        t_start = time.time()
        try:
            out = fn()
            mt, mf = out[0], out[1]
            if len(out) > 2:
                stats[name] = out[2]
            tests += mt
            findings += mf
            stats["modules"][name] = {"tests": len(mt), "findings": len(mf), "seconds": round(time.time() - t_start, 1)}
            events(name, f"{name}: {len(mt)} tests, {len(mf)} findings in {time.time() - t_start:.0f}s")
        except Exception as exc:
            logger.exception("module %s failed", name)
            stats["modules"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            events(name, f"{name} module failed: {type(exc).__name__}: {str(exc)[:200]}", None, level="warn")
            tests.append(TestRecord(test_key=f"module|{name}", name=f"{name} module", technique=name, category="platform", route="", expected="Module completes",
                                    observed=f"{type(exc).__name__}: {str(exc)[:300]}", status="blocked"))
    return tests, findings, stats


class RunContext:
    def __init__(self, settings: Settings, run_id: str):
        self.settings = settings
        self.run_id = run_id

    def event(self, stage: str, message: str, progress: int | None = None, level: str = "info") -> None:
        logger.info("[%s] %s: %s", self.run_id, stage, message)
        with session(self.settings.database_url) as db:
            run = db.get(Run, self.run_id)
            if run is None:
                return
            run.stage = stage
            run.message = message[:500]
            if progress is not None:
                run.progress = max(run.progress, min(progress, 100))
            db.add(RunEvent(run_id=self.run_id, level=level, stage=stage, message=message[:2000]))
            db.commit()


def run_pipeline(run_id: str, settings: Settings) -> None:
    ctx = RunContext(settings, run_id)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.progress = 1
        db.commit()
        repo_url, live_url, options, project = run.repo_url, run.live_url, dict(run.options or {}), run.project
        secrets = decrypt_secrets(settings.secret_key, run.secrets)

    run_dir = settings.artifacts_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        # 1. ingest ---------------------------------------------------------
        ctx.event("ingest", f"Fetching codebase {repo_url}", 3)
        repo = fetch_repo(repo_url, settings.artifacts_dir / "repos" / run_id, token=settings.github_token or secrets.get("github_token"), branch=options.get("branch") or None)
        ctx.event("ingest", f"Detected {repo.framework} ({repo.router or 'no router'}) at {repo.commit_sha[:10]}; parsing source", 6)
        inv = build_inventory(repo.path, repo.framework)
        api_index = _api_index(inv)
        intents = {el.key: build_intent(el, inv, api_index) for el in inv.elements}
        interactive = [e for e in inv.elements if e.kind in INTERACTIVE_KINDS]
        ctx.event("ingest", f"Parsed {inv.files_parsed} files: {len(inv.routes)} routes, {len(interactive)} interactive components, {len(inv.api_calls)} API calls", 12)
        if settings.llm_intent and settings.gemini_api_key and options.get("llm_intent", True):
            ctx.event("ingest", f"Summarising page intent with {settings.gemini_model}", 14)
            enrichment = enrich_with_llm(inv, repo.path, settings.artifacts_dir / "cache" / "intent", settings.gemini_api_key, settings.gemini_model, fallback_model=settings.gemini_fallback_model)
            apply_enrichment(inv.elements, intents, enrichment)
            failed = enrichment.pop("__failures__", None)
            ctx.event("ingest", f"Intent summaries for {len(enrichment)} files" + (f"; {failed['count']} files failed (quota or outage): {failed['first'][0][:120]}" if failed else ""), 18, level="warn" if failed else "info")
        (run_dir / "inventory.json").write_text(json.dumps({**inv.to_dict(), "intents": intents}, indent=1, default=str), encoding="utf-8")
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            r.commit_sha = repo.commit_sha
            r.framework = repo.framework
            db.commit()

        # 2. explore --------------------------------------------------------
        ctx.event("explore", f"Opening {live_url}", 20)
        max_pages = int(options.get("max_pages") or settings.max_pages)
        settings_local = Settings(**{**asdict(settings), "max_pages": max_pages})
        ex = explore(settings_local, live_url, inv.routes, secrets.get("email", ""), secrets.get("password", ""), run_dir, ctx.event)
        for w in ex.warnings:
            ctx.event("explore", w, None, level="warn")
        if ex.login is not None and not ex.login.success:
            raise RuntimeError(f"Could not sign in to {live_url}: {ex.login.error}")
        (run_dir / "explore.json").write_text(json.dumps({"pages": [p.to_dict() for p in ex.pages], "guards": [asdict(g) for g in ex.guards], "warnings": ex.warnings}, indent=1, default=str), encoding="utf-8")
        ctx.event("explore", f"Explored {len(ex.pages)} pages, {sum(len(p.elements) for p in ex.pages)} controls observed", 56)

        # 3. reconcile ------------------------------------------------------
        protected_routes = [r.path for r in inv.routes if r.protected]
        rec = reconcile(inv.elements, ex.pages, protected_routes, ex.origin)
        components = build_components(rec, intents)
        ctx.event("reconcile", f"Matched {rec.stats['matched']}/{rec.stats['code_interactive']} code components on the live app ({rec.coverage * 100:.0f}%); {rec.stats['observed_unmatched_visible']} live controls have no code declaration", 59)

        # 4-5. execute + judge --------------------------------------------------
        try:
            tests, findings, phase_stats = execute_all(settings, live_url, secrets, run_dir, inv, ex, components, protected_routes, options, ctx.event, tag=run_id)
        finally:
            state_file = Path(ex.storage_state_path) if ex.storage_state_path else None
            if state_file and state_file.exists():
                state_file.unlink()  # holds a live refresh token; never keep it after the run
        # findings that are not click-based are deterministic single observations
        for f in findings:
            if f.reproduced_m == 0:
                f.reproduced_n, f.reproduced_m = 1, 1
        ctx.event("judge", f"{len(tests)} tests, {len({f.fingerprint for f in findings})} candidate findings; {sum(1 for f in findings if f.status != 'reported')} held for review", 92)

        # 6. report ---------------------------------------------------------
        finished = datetime.now(timezone.utc)
        stats = {
            **rec.stats, "coverage": round(rec.coverage, 4), "coverage_unconditional": round(rec.coverage_unconditional, 4),
            **coverage_after(components), **phase_stats,
            "pages": len(ex.pages), "routes": len(inv.routes), "tests": len(tests),
            "tests_by_status": {s: sum(1 for t in tests if t.status == s) for s in {t.status for t in tests}},
            "components_by_status": {s: sum(1 for c in components if c.status == s) for s in {c.status for c in components}},
            "duration_seconds": round(time.time() - t0, 1),
        }
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            extra = persist(db, r, ex.pages, components, tests, findings)
            stats.update(extra)
            r.stats = stats
            r.status = "completed"
            r.finished_at = finished
            r.progress = 100
            r.stage = "done"
            r.message = f"{extra['findings_total']} findings; {stats['coverage_interaction'] * 100:.0f}% of code components found, {stats['tested_ratio'] * 100:.0f}% tested"
            r.secrets = None
            db.commit()
        report_path = write_report(run_dir, run_id, {"live_url": live_url, "repo_url": repo_url, "commit_sha": repo.commit_sha, "framework": repo.framework, "finished_at": finished.isoformat()}, stats, ex.pages, components, tests, findings)
        ctx.event("done", f"Report written: {report_path}", 100)
    except Exception as exc:
        logger.exception("run %s failed", run_id)
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            if r is not None:
                r.status = "failed"
                r.error = f"{type(exc).__name__}: {str(exc)[:900]}\n{traceback.format_exc()[-1500:]}"
                r.finished_at = datetime.now(timezone.utc)
                r.secrets = None
                r.message = f"failed in {r.stage}: {type(exc).__name__}: {str(exc)[:200]}"
                db.add(RunEvent(run_id=run_id, level="error", stage=r.stage, message=r.message))
                db.commit()
