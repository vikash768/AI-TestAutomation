"""The seven-stage run, from a queued Run row to a finished report."""
from __future__ import annotations

import json
import re
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
            row.reveal = list(m.page.reveal)
            row.visible, row.enabled, row.in_dialog = o.visible, o.enabled, o.in_dialog
            row.attrs["haspopup"] = o.haspopup
            row.notes = f"matched by {m.reason}"
        else:
            row.status = "not_observed"
            row.notes = "declared in code; not found on the live page" + (" (conditional or data-driven)" if (c.conditional or c.repeated) else "")
        components.append(row)
    seen_obs: dict[tuple, ComponentRecord] = {}
    for page, o in rec.unmatched_observed:
        sig = (page.route, o.role, re.sub(r"\d{4,}", "#", normalize(o.name)) or o.selector)  # per-row controls collapse to one
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
            in_dialog=o.in_dialog, reveal=list(page.reveal), notes="present on live page, not declared in code",
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
        "revealed_components": sum(1 for c in components if any(not s.get("nav") for s in c.reveal)),
        "tested_components": len(tested),
        "equivalent_components": sum(1 for c in components if c.status == "equivalent"),
        "tested_ratio": round(len(tested) / len(exercisable), 4) if exercisable else 0.0,
    }


PHASE2_MODULES = ("reveal", "destructive", "forms", "auth", "state", "lifecycle", "faults")
# Checklist buckets the user asked Grafana to show, mapped from the fine-grained category of each test/finding.
CHECKLIST = {
    "functional": [("functional", None), ("navigation", None), ("persistence", None), ("lifecycle", None), ("validation", None)],
    "ui": [("ui-responsive", None), ("accessibility", None)],
    "security": [("security", None)],
    "load": [("performance", "load"), ("performance", None)],
    "unit": [("unit", None)],
    "stability": [("stability", None), ("ux", None)],
}
CHECKLIST_LABEL = {"functional": "Functional (code-derived)", "ui": "UI & resize / accessibility", "security": "Security / penetration (non-destructive)",
                   "load": "Load / performance", "unit": "Unit tests (codebase)", "stability": "Stability & UX"}


def _category_of_bucket(cat: str, technique: str) -> str:
    for bucket, rules in CHECKLIST.items():
        for rc, rt in rules:
            if cat == rc and (rt is None or rt == technique):
                return bucket
    return "stability"


def finalize_categories(stats: dict, tests: list, findings: list) -> dict:
    buckets: dict[str, dict] = {b: {"tests": 0, "pass": 0, "fail": 0, "blocked": 0, "findings": 0, "ran": False} for b in CHECKLIST}
    for t in tests:
        b = _category_of_bucket(t.category, t.technique)
        d = buckets.setdefault(b, {"tests": 0, "pass": 0, "fail": 0, "blocked": 0, "findings": 0, "ran": False})
        d["tests"] += 1
        d["ran"] = True
        d[t.status] = d.get(t.status, 0) + 1
    for f in findings:
        b = _category_of_bucket(f.category, "")
        buckets.setdefault(b, {"tests": 0, "pass": 0, "fail": 0, "blocked": 0, "findings": 0, "ran": True})["findings"] += 1
    stats["categories"] = buckets
    return stats


def dead_screen_checks(inv) -> tuple[list, list]:
    """Screens present in the repo that no import chain from the app entry reaches.
    The live app can never show them, so they are reported (not counted as untested)."""
    from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
    tests, findings = [], []
    for f in getattr(inv, "dead_files", []) or []:
        screen = Path(f).stem
        tests.append(TestRecord(test_key=f"reachability|{f}", name=f"{screen} screen is reachable from the app entry", technique="static-reachability",
                                category="functional", route="(codebase)", expected="Imported (directly or transitively) by the app entry",
                                observed="Nothing imports this file", status="fail"))
        findings.append(FindingRecord(
            fingerprint=fingerprint("dead-screen", f), route="(codebase)", category="functional", severity="low",
            title=f"Screen '{screen}' is in the codebase but can never be shown",
            description=f"{f} defines a screen, but no file reachable from the app entry imports it, so users cannot open it. "
                        "Either it is dead code or the feature was never wired into navigation.",
            expected="Every screen in the codebase is reachable from the app's navigation", observed=f"{f}: imported by nothing",
            standard_ref="ISTQB static testing (unreachable code)", confidence=0.95, reproduced_n=1, reproduced_m=1, test_key=f"reachability|{f}"))
    return tests, findings


def execute_all(settings: Settings, live_url: str, secrets: dict, run_dir: Path, inv, ex, components: list[ComponentRecord], protected_routes: list[str],
                options: dict, events, tag: str = "run", repo_path: "Path | None" = None) -> tuple[list, list, dict]:
    """Phase 1 checks and smoke clicks, then the Phase 2 modules. A module that fails is recorded, not fatal."""
    from qaplatform.execute.destructive import run_destructive
    from qaplatform.execute.faults import run_faults
    from qaplatform.execute.forms import FormTester, auth_tests
    from qaplatform.execute.lifecycle import run_lifecycle
    from qaplatform.execute.reveal import Revealer
    from qaplatform.execute.security import detect_api_base, run_security
    from qaplatform.execute.load import run_load
    from qaplatform.execute.unit import run_unit
    from qaplatform.execute.provision import run_provision
    from qaplatform.execute.smoke import TestRecord
    from qaplatform.execute.state import run_state

    tests, findings = guard_tests(ex.guards)
    t2, f2 = page_tests(ex.pages, settings)
    tests += t2
    findings += f2
    t2, f2 = dead_screen_checks(inv)
    tests += t2
    findings += f2
    stats: dict = {"modules": {}}
    api_base = detect_api_base(inv, live_url)
    default_enabled = set(PHASE2_MODULES) | {"security"}
    if options.get("load") and options.get("load_authorized"):
        default_enabled.add("load")
    if options.get("unit"):
        default_enabled.add("unit")
    if options.get("provision"):
        default_enabled.add("provision")
    enabled = set(options.get("modules") or default_enabled)
    if not options.get("phase2", True):
        # unticking interaction tests drops only those; security and the opt-ins (load, unit, provision) stay
        enabled -= set(PHASE2_MODULES)
    if not options.get("load_authorized"):
        enabled.discard("load")  # load traffic needs the operator's explicit authorisation, however modules were chosen

    def _run(name, fn):
        if name not in enabled:
            return
        import time as _t
        t0 = _t.time()
        try:
            out = fn()
            mt, mf = out[0], out[1]
            if len(out) > 2 and out[2]:
                stats[name] = out[2]
            tests.extend(mt)
            findings.extend(mf)
            stats["modules"][name] = {"tests": len(mt), "findings": len(mf), "seconds": round(_t.time() - t0, 1)}
            events(name, f"{name}: {len(mt)} tests, {len(mf)} findings in {_t.time() - t0:.0f}s")
        except Exception as exc:
            logger.exception("module %s failed", name)
            stats["modules"][name] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            events(name, f"{name} module failed: {type(exc).__name__}: {str(exc)[:200]}", None, level="warn")
            tests.append(TestRecord(test_key=f"module|{name}", name=f"{name} module", technique=name, category="platform", route="", expected="Module completes",
                                    observed=f"{type(exc).__name__}: {str(exc)[:300]}", status="blocked"))

    # Creds-independent: security assessment and the codebase's own unit tests run with or without a login.
    _run("security", lambda: run_security(settings, live_url, inv, ex.pages, api_base, events))
    if repo_path is not None:
        _run("unit", lambda: run_unit(repo_path, tag, events, env={"DATABASE_URL": settings.database_url.replace("+psycopg", "").replace("@localhost", "@postgres")} if options.get("unit_db") else None))

    events("execute", f"{len(tests)} checks done; starting smoke clicks", 60)
    if not (ex.storage_state_path or secrets.get("email")):
        # No test account: public endpoints can still be load-tested, unauthenticated.
        _run("load", lambda: run_load(settings, live_url, ex.pages, api_base, {}, events, **_load_kwargs(options), auth_kind="none"))
        return tests, findings, finalize_categories(stats, tests, findings)
    runner = SmokeRunner(settings, live_url, ex.storage_state_path, run_dir, secrets, events)
    t3, f3 = runner.run(components, ex.pages, protected_routes)
    tests += t3
    findings += f3
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
        ("load", lambda: _load_with_session(runner, settings, live_url, ex.pages, api_base, options, events)),
        ("provision", lambda: run_provision(runner, components, ex.pages, protected_routes, login_path, events, tag)),
    ]
    for name, fn in plan:
        _run(name, fn)
    credit_signin_components(components, ex.pages, tests)
    credit_session_enders(components, tests)
    credit_shared_internals(components, inv)
    return tests, findings, finalize_categories(stats, tests, findings)


def _load_with_session(runner, settings, live_url, pages, api_base, options, events):
    """Load test with a fresh signed-in session (the app's own auth header or cookies), so
    protected endpoints are measured for real instead of answering 401 under load."""
    from qaplatform.execute.load import run_load
    headers, how = runner.capture_auth_headers(api_base)
    events("load", f"Load test credentials: {how}" + ("" if headers else " (protected endpoints will be reported as not measurable)"), 90)
    return run_load(settings, live_url, pages, api_base, headers, events,
                    **_load_kwargs(options), auth_kind=how)


def _load_kwargs(options: dict) -> dict:
    capacity = options.get("load_mode") == "capacity"
    return {"users": int((options.get("load_max_users") if capacity else options.get("load_users")) or (50 if capacity else 10)),
            "seconds": int(options.get("load_seconds") or 60), "p95_ms": int(options.get("load_p95_ms") or 2000),
            "mode": "capacity" if capacity else "ramp", "step_seconds": int(options.get("load_step_seconds") or 15),
            "expected_users": int(options.get("load_expected_users") or 0)}


def credit_session_enders(components: list, tests: list) -> None:
    """The logout test clicks the app's sign-out control (finding it in a profile menu if
    needed); every sign-out control takes that test's outcome."""
    t = next((t for t in tests if t.test_key.startswith("logout|") and t.status in ("pass", "fail")), None)
    if t is None:
        return
    used = t.test_key.split("|", 1)[1]
    for c in components:
        if c.action_class == "session_ending" and c.status in ("observed", "untested", "skipped") and (c.key == used or c.page_url):
            c.status = t.status
            c.notes = (c.notes + "; " if c.notes else "") + f"exercised by '{t.name}' ({t.status})"


def credit_shared_internals(components: list, inv) -> None:
    """Controls inside a shared component's own file (<Dropdown>'s trigger and options) are not
    separate features: they are exercised through each place the component is used. When those
    usages were tested, the internals are marked equivalent instead of 'not found'."""
    from pathlib import PurePosixPath
    tag_of = {e.key: e.tag for e in inv.elements}
    tested_usages: dict[str, int] = {}
    for c in components:
        tag = tag_of.get(c.key) or ""
        if tag[:1].isupper() and c.status in ("pass", "fail"):
            tested_usages[tag] = tested_usages.get(tag, 0) + 1
    for c in components:
        if c.status != "not_observed" or not c.code_file:
            continue
        comp = PurePosixPath(c.code_file).stem
        if tested_usages.get(comp):
            # annotated only: nothing clicked THIS element, so it is not counted as tested
            c.notes = f"inside the shared <{comp}> component, whose {tested_usages[comp]} usage(s) were tested; this element itself was not found"


def credit_signin_components(components: list, pages: list, tests: list) -> None:
    """Sign-in screen controls are exercised by the signed-out sign-in tests; give the controls
    those tests actually use (identifier, password, submit, remember-me) that test's outcome.
    Anything else on the screen (forgot-password link, show-password toggle) gets no credit."""
    from qaplatform.execute.smoke import on_signin_screen
    by_key = {t.test_key: t for t in tests if t.test_key.startswith("auth|")}
    for c in components:
        if not on_signin_screen(c, pages):
            continue
        label = (c.label or c.name or "").lower()
        ftype = str((c.attrs or {}).get("type") or "").lower()
        if c.kind == "checkbox" and re.search(r"remember|keep me|stay signed", label):
            t = by_key.get("auth|remember")
        elif c.kind in ("button", "clickable") and re.fullmatch(r"(sign ?in|log ?in|continue|submit)", label.strip()):
            t = by_key.get("auth|unknown")
        elif c.kind in ("input",) and (ftype in ("email", "password") or re.search(r"\b(email|password|username|user name)\b", label)):
            t = by_key.get("auth|enter")
        else:
            t = None
        if t is None or t.status not in ("pass", "fail"):
            continue
        c.status = t.status
        c.source = "both" if c.source == "code" else c.source
        c.notes = f"exercised by the sign-in test '{t.name}' ({t.status})"


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



def _short_repo_name(url: str) -> str:
    base = url.rstrip("/").split("/")[-1]
    return base[:-4] if base.endswith(".git") else base or "repo"


def merge_inventories(invs: list) -> "CodeInventory":
    """Combine several repos' inventories into one. Each element/call keeps its
    repo as a prefix on its file path, so provenance and keys stay unique when a
    frontend and a backend are tested together."""
    from qaplatform.ingest.js_parser import CodeInventory
    merged = CodeInventory(framework="+".join(sorted({i.framework for i, _ in invs})) or "unknown")
    for inv, name in invs:
        for el in inv.elements:
            el.file = f"{name}/{el.file}"
            el.key = f"{name}::{el.key}"
            merged.elements.append(el)
        for r in inv.routes:
            r.file = f"{name}/{r.file}"
            merged.routes.append(r)
        for a in inv.api_calls:
            a.file = f"{name}/{a.file}"
            merged.api_calls.append(a)
        for coll in ("timers", "storage", "navigates"):
            for item in getattr(inv, coll):
                item.file = f"{name}/{item.file}"
                getattr(merged, coll).append(item)
        merged.wrappers.update(inv.wrappers)
        for route, pf in inv.page_files.items():
            merged.page_files[route] = f"{name}/{pf}"
        merged.files_parsed += inv.files_parsed
        merged.dead_files.extend(f"{name}/{d}" for d in getattr(inv, "dead_files", []))
        merged.warnings.extend(f"[{name}] {w}" for w in inv.warnings)
    merged.routeless = any(i.routeless for i, _ in invs) or not merged.routes
    return merged


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
        # 1. ingest (one or more repos) -------------------------------------
        repo_urls = [u for u in (options.get("repos") or [repo_url]) if u]
        token = settings.github_token or secrets.get("github_token")
        repos = []
        for i, url in enumerate(repo_urls):
            name = _short_repo_name(url)
            ctx.event("ingest", f"Fetching codebase {i + 1}/{len(repo_urls)}: {name}", 3)
            info = fetch_repo(url, settings.artifacts_dir / "repos" / run_id / name, token=token, branch=options.get("branch") or None)
            one = build_inventory(info.path, info.framework)
            ctx.event("ingest", f"{name}: {info.framework} ({info.router or 'no router'}) @ {info.commit_sha[:10]} — {one.files_parsed} files, {len(one.routes)} routes, {len([e for e in one.elements if e.kind in INTERACTIVE_KINDS])} components, {len(one.api_calls)} API calls", 6)
            repos.append({"url": url, "name": name, "info": info, "inv": one})
        primary = repos[0]["info"]
        inv = repos[0]["inv"] if len(repos) == 1 else merge_inventories([(r["inv"], r["name"]) for r in repos])
        primary_path = repos[0]["info"].path
        api_index = _api_index(inv)
        intents = {el.key: build_intent(el, inv, api_index) for el in inv.elements}
        interactive = [e for e in inv.elements if e.kind in INTERACTIVE_KINDS]
        ctx.event("ingest", f"Parsed {len(repos)} repo(s): {inv.files_parsed} files, {len(inv.routes)} routes, {len(interactive)} interactive components, {len(inv.api_calls)} API calls", 12)
        if settings.llm_intent and settings.gemini_api_key and options.get("llm_intent", True):
            ctx.event("ingest", f"Summarising page intent with {settings.gemini_model}", 14)
            for r in repos:
                enrichment = enrich_with_llm(r["inv"], r["info"].path, settings.artifacts_dir / "cache" / "intent", settings.gemini_api_key, settings.gemini_model, fallback_model=settings.gemini_fallback_model)
                enrichment.pop("__failures__", None)
                apply_enrichment([e for e in inv.elements if e.file.startswith(r["name"] + "/")], intents, {f"{r['name']}/{k}": v for k, v in enrichment.items()})
        (run_dir / "inventory.json").write_text(json.dumps({**inv.to_dict(), "intents": intents, "repos": [r["url"] for r in repos]}, indent=1, default=str), encoding="utf-8")
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            r.commit_sha = primary.commit_sha
            r.framework = inv.framework
            db.commit()
        repo = primary

        # 2. explore --------------------------------------------------------
        ctx.event("explore", f"Opening {live_url}", 20)
        max_pages = int(options.get("max_pages") or settings.max_pages)
        settings_local = Settings(**{**asdict(settings), "max_pages": max_pages})
        ex = explore(settings_local, live_url, inv.routes, secrets.get("email", ""), secrets.get("password", ""), run_dir, ctx.event, routeless=getattr(inv, "routeless", False))
        for w in ex.warnings:
            ctx.event("explore", w, None, level="warn")
        if secrets.get("email") and ex.login is not None and not ex.login.success:
            raise RuntimeError(f"Could not sign in to {live_url}: {ex.login.error}")
        if not secrets.get("email"):
            ctx.event("explore", "No test account given; running public surface, security and unit only (authenticated pages skipped)", None, level="warn")
        (run_dir / "explore.json").write_text(json.dumps({"pages": [p.to_dict() for p in ex.pages], "guards": [asdict(g) for g in ex.guards], "warnings": ex.warnings}, indent=1, default=str), encoding="utf-8")
        ctx.event("explore", f"Explored {len(ex.pages)} pages, {sum(len(p.elements) for p in ex.pages)} controls observed", 56)

        # 3. reconcile ------------------------------------------------------
        protected_routes = [r.path for r in inv.routes if r.protected]
        rec = reconcile(inv.elements, ex.pages, protected_routes, ex.origin)
        components = build_components(rec, intents)
        ctx.event("reconcile", f"Matched {rec.stats['matched']}/{rec.stats['code_interactive']} code components on the live app ({rec.coverage * 100:.0f}%); {rec.stats['observed_unmatched_visible']} live controls have no code declaration", 59)

        # 4-5. execute + judge --------------------------------------------------
        try:
            tests, findings, phase_stats = execute_all(settings, live_url, secrets, run_dir, inv, ex, components, protected_routes, options, ctx.event, tag=run_id, repo_path=primary_path)
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
