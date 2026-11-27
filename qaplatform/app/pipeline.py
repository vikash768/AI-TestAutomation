"""The native app run (android / ios / macos): the same seven stages as the web
pipeline, with Appium in place of Playwright.

  1 ingest     clone the repo, extract screens / controls / menus from the code
  2 explore    install or launch the build, sign in, walk every screen, tap every safe control
  3 reconcile  match code controls to the controls seen on each screen (lead's matcher)
  4 execute    static screen checks, tap results, journeys with QA test data (planned from the code and,
               with GEMINI_API_KEY, by the LLM; every step checked by the safety rules), cleanup of that data,
               active device checks, and an optional bounded GET-only load test of the backend API
  5 judge      failing taps reproduced twice from a fresh launch; confidence gate; LLM second look (needs_review only)
  6 report     same tables, HTML report and Grafana dashboards as web runs

The codebase can be one source (the app) or two (the app, then its backend). The backend's routes are used for
journey planning and load-test targets only; they never count towards UI component coverage.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone

from qaplatform.app import ai as native_ai
from qaplatform.app.assertions import check_api_assertion, check_ui_assertion
from qaplatform.app.backend import BackendRoute, ClientEndpoint, backend_routes, base_urls, client_endpoints, summarize
from qaplatform.app.build import resolve_build
from qaplatform.app.buildcheck import build_checks
from qaplatform.app.checks import ActiveChecks, code_checks, screen_checks, signin_negative, tap_results
from qaplatform.app.code import app_intent, build_app_inventory
from qaplatform.app.device import AppDevice
from qaplatform.app.explore import SKIP_SETUP_RE, AppExplorer, Screen
from qaplatform.app.journeys import JourneyRunner, journeys_from_ai, plan_dump, plan_from_code, to_tests, validate
from qaplatform.app.journeys import summary as journey_summary
from qaplatform.app.loadtest import LoadConfig, run_load_test
from qaplatform.app.remedy import explain
from qaplatform.app.testdata import QADataRegistry
from qaplatform.app.uploads import apk_upload_path
from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.config import SCOPED_APP_PLATFORMS, Settings
from qaplatform.db.models import Run, RunEvent
from qaplatform.db.session import session
from qaplatform.explore.crawler import ObservedPage
from qaplatform.explore.safety import classify_action
from qaplatform.ingest.js_parser import CodeInventory
from qaplatform.ingest.repo import fetch_repo
from qaplatform.ingest.workspace import repository_workspace
from qaplatform.pipeline import RunContext, build_components, coverage_after
from qaplatform.reconcile.matcher import INTERACTIVE_KINDS, normalize, reconcile
from qaplatform.report.html import write_report
from qaplatform.report.writer import persist
from qaplatform.secrets import decrypt_secrets

logger = logging.getLogger(__name__)

APP_MODULES = ("launch", "rotation", "resume", "relaunch", "offline", "inputs", "auth", "perf")
CLASS_ORDER = ["safe", "download", "external", "system", "mutating", "session_ending", "destructive"]
HELD_REASON = {"mutating": "changes or sends data", "destructive": "deletes or resets data", "session_ending": "signs the user out", "external": "leaves the app",
               "system": "added by the operating system", "download": "downloads a file", "auth": "sign-in submit; covered by the sign-in tests", "skipped": "unnamed control"}


def held_actions(explorer: AppExplorer) -> list[dict]:
    route_of = {s.sig: s.route or s.snap.title for s in explorer.screens}
    return [{"screen": route_of.get(t.screen_sig, ""), "label": t.element.name, "class": t.action_class, "reason": t.detail or HELD_REASON.get(t.action_class, "held by the safety rules")}
            for t in explorer.taps if t.result == "skipped" and t.action_class != "safe"]


def _words(name: str) -> str:
    return normalize(re.sub(r"([a-z])([A-Z])", r"\1 \2", name or ""))


def label_classes(inv: CodeInventory, intents: dict) -> dict[str, str]:
    """Normalized label -> the most cautious class any code control with that label has."""
    out: dict[str, str] = {}
    for el in inv.elements:
        if not el.label:
            continue
        cls = classify_action(el.label, el.attrs, intents.get(el.key, {}))
        key = normalize(el.label)
        if CLASS_ORDER.index(cls) > CLASS_ORDER.index(out.get(key, "safe")):
            out[key] = cls
    return out


def assign_routes(screens: list[Screen], inv: CodeInventory, texts_by_screen: dict[str, set[str]]) -> None:
    """Name each observed screen after the code screen it is. Evidence, strongest first: the control tapped to reach it
    navigates there in the code; the screen's title is that screen's name; labels and texts only that code screen has."""
    code_tokens: dict[str, set[str]] = {}
    for r in inv.routes:
        toks = {normalize(t) for t in texts_by_screen.get(r.path, set())}
        toks |= {normalize(e.label) for e in inv.elements if e.route == r.path and e.label}
        code_tokens[r.path] = {t for t in toks if t}
    spread: dict[str, int] = {}
    for toks in code_tokens.values():
        for t in toks:
            spread[t] = spread.get(t, 0) + 1
    nav_by_label: dict[str, set[str]] = {}
    for e in inv.elements:
        for label in ([e.label] + list(e.label_alternatives or [])) if e.navigates_to else []:
            if label:
                nav_by_label.setdefault(normalize(label), set()).update(t for t in e.navigates_to if t in code_tokens)
    by_path = {tuple(e.name for e in s.path): s for s in screens}
    used_titles: dict[str, int] = {}
    for s in screens:
        if s.route:
            continue
        seen = {normalize(e.name) for e in s.snap.elements if e.name} | {normalize(t) for t in s.snap.texts}
        heading = {normalize(s.snap.title)} | {normalize(t) for t in s.snap.texts[:3]}
        reached_by = nav_by_label.get(normalize(s.path[-1].name), set()) if s.path and s.path[-1].name and s.kind == "screen" else set()
        best, best_score = None, 0.0
        for route, toks in code_tokens.items():
            score = sum(1.0 / spread[t] for t in seen & toks)  # a label shared by many screens says little
            if _words(route) in heading:
                score += 3
            if route in reached_by:
                score += 6 / len(reached_by)
            if score > best_score:
                best, best_score = route, score
        parent = by_path.get(tuple(e.name for e in s.path[:-1])) if s.path and not reached_by else None
        if best is not None and best_score >= 1.5:
            s.route = best
        elif parent is not None and parent.route and not parent.route.startswith("screen:"):
            s.route = parent.route  # opened in place (a panel, sheet or filter) by a control that does not navigate
        else:
            title = re.sub(r"[^A-Za-z0-9 ]+", "", s.snap.title).strip()[:40] or "screen"
            used_titles[title] = used_titles.get(title, 0) + 1
            s.route = f"screen:{title}" + (f" {used_titles[title]}" if used_titles[title] > 1 else "")


def to_pages(screens: list[Screen], app_id: str, protected_routes: set[str], signed_in: bool, platform: str, taps) -> list[ObservedPage]:
    pages = []
    errors_by_sig: dict[str, list[str]] = {}
    for t in taps:
        errors_by_sig.setdefault(t.screen_sig, []).extend(t.errors)
    for s in screens:
        elements = list(s.snap.elements)
        if platform == "macos" and s.kind == "screen" and s.depth == 0:
            bar = [e for e in elements if e.tag == "MenuBarItem"]
            pages.append(ObservedPage(route="menubar", url=f"app://{app_id}/menubar#{s.sig}", title="Menu bar", protected=True, elements=bar, screenshot=s.screenshot))
            elements = [e for e in elements if e.tag not in ("MenuBarItem", "MenuItem")]
        shot = {"device": {"screenshot": s.screenshot, "width": s.snap.width, "height": s.snap.height}} if s.screenshot else {}
        pages.append(ObservedPage(route=s.route, url=f"app://{app_id}/{s.route}#{s.sig}", final_url=s.snap.activity, title=s.snap.title, protected=(s.route in protected_routes) or (signed_in and s.depth > 0),
                                  elements=elements, console_errors=list(dict.fromkeys(errors_by_sig.get(s.sig, [])))[:20], viewports=shot, screenshot=s.screenshot, load_ms=s.load_ms,
                                  state={"kind": s.kind, "path": [e.name for e in s.path]}))
    return pages


def _persist_results(ctx: RunContext, stats: dict, pages: list, components: list, tests: list, findings: list, summary: str) -> datetime:
    ctx.event("judge", f"{len(tests)} tests, {len({f.fingerprint for f in findings})} candidate findings; {sum(1 for f in findings if f.status != 'reported')} held for review", 92)
    finished = datetime.now(timezone.utc)
    stats.update(tests=len(tests), tests_by_status={status: sum(t.status == status for t in tests) for status in {t.status for t in tests}})
    with session(ctx.settings.database_url) as db:
        run = db.get(Run, ctx.run_id)
        stats.update(persist(db, run, pages, components, tests, findings))
        run.stats, run.status, run.finished_at, run.progress, run.stage = stats, "completed", finished, 100, "done"
        run.message = f"{stats['findings_total']} findings; {summary}"
        run.secrets = None
        db.commit()
    return finished


def _leg_result(tests: list[TestRecord], required: list[TestRecord]) -> dict:
    failed = sum(t.status in ("fail", "flaky") for t in tests)
    passed = sum(t.status == "pass" for t in tests)
    blocked = sum(t.status == "blocked" for t in tests)
    status = "fail" if failed else "blocked" if not passed or any(t.status == "blocked" for t in required) else "pass"
    detail = f"{passed} passed, {failed} failed, {blocked} blocked"
    if (unavailable := next((t for t in required if t.status == "blocked"), None)) is not None:
        detail += f". {unavailable.observed}"
    return {"status": status, "detail": detail}


def _blocked_check(key: str, name: str, technique: str, route: str, reason: str) -> TestRecord:
    return TestRecord(test_key=key, name=name, technique=technique, category="functional", route=route,
                      observed=reason, status="blocked")


def _append_assertion(test: TestRecord, tests: list[TestRecord], findings: list[FindingRecord]) -> None:
    tests.append(test)
    if test.status == "fail":
        findings.append(FindingRecord(
            fingerprint=fingerprint(test.route, "functional", "expected result", test.test_key), route=test.route,
            category="functional", severity="medium", title=f"Expected result failed: {test.name}",
            expected=test.expected, observed=test.observed, standard_ref="User-defined expectation", confidence=0.8,
            viewport="device" if test.route != "backend" else "backend", status="needs_review",
            evidence=test.evidence, steps=test.steps, test_key=test.test_key,
        ))


def _combined_assertion(ui: TestRecord, api: TestRecord) -> TestRecord:
    statuses = {ui.status, api.status}
    status = "fail" if "fail" in statuses else "blocked" if "blocked" in statuses else "pass"
    return TestRecord(test_key="full|assertions", name="Combined app and API expectations", technique="functional", category="functional",
                      route="full", expected="Both submitted expectations pass", observed=f"App check: {ui.status}. API check: {api.status}.",
                      status=status, evidence=ui.evidence)


def run_app_pipeline(run_id: str, settings: Settings) -> None:
    ctx = RunContext(settings, run_id)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        run.status, run.started_at, run.progress = "running", datetime.now(timezone.utc), 1
        db.commit()
        repo_url, build_spec, options, platform = run.repo_url, run.live_url, dict(run.options or {}), run.platform
        secrets = decrypt_secrets(settings.secret_key, run.secrets)

    test_mode = options.get("test_mode") if platform in SCOPED_APP_PLATFORMS else None
    backend_required = test_mode in ("backend", "full")
    build_only = test_mode == "frontend" or (platform in SCOPED_APP_PLATFORMS and test_mode is None and not repo_url)
    uploaded_apk = apk_upload_path(settings.artifacts_dir, run_id) if platform == "android" and options.get("uploaded_apk") else None
    run_dir = settings.artifacts_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    build = None
    dev: AppDevice | None = None
    runner: JourneyRunner | None = None
    registry = QADataRegistry(run_id, run_dir / "qa_data.json")
    completed = False
    terminal = False
    checkouts = ExitStack()
    legs: dict = {}
    assertions = options.get("assertions", {}) if test_mode else {}
    api_result = ui_result = None
    frontend_pending = False
    platform_label = "macOS" if platform == "macos" else "Android"
    frontend_stage = f"Prepare the {platform_label} build"
    pages, components, tests, findings = [], [], [], []
    try:
        # 1. ingest ---------------------------------------------------------
        inv, texts_by_screen, commit_sha, framework, repo_path = CodeInventory(), {}, "", "", None
        token = secrets.get("github_token")
        sources = [dict(s) for s in options.get("codebase_sources") or ([{"role": "frontend", "source": repo_url}] if repo_url else [])]
        if test_mode == "frontend":
            sources = []
        elif backend_required:
            sources = [src for src in sources if src["role"] == "backend"]
        checkout_dir = None
        frontend = next((src for src in sources if src["role"] == "frontend"), None)
        if frontend:
            ctx.event("ingest", f"Fetching codebase {frontend['source']}", 3)
            try:
                checkout_dir = checkouts.enter_context(repository_workspace(settings.artifacts_dir))
                repo = fetch_repo(frontend["source"], checkout_dir / "frontend", token=token, branch=options.get("branch") or None)
                frontend["commit_sha"] = repo.commit_sha
                commit_sha, repo_path = repo.commit_sha, repo.path
                inv, texts_by_screen = build_app_inventory(repo.path, platform)
                framework = inv.framework
                ctx.event("ingest", f"Parsed {inv.files_parsed} files ({inv.framework}): {len(inv.routes)} screens, {len([e for e in inv.elements if e.kind in INTERACTIVE_KINDS])} controls, {len(inv.api_calls)} API calls", 12)
            except Exception as exc:
                # The build can still be tested; coverage against the code is then unavailable.
                ctx.event("ingest", f"Codebase unavailable, testing the build only: {str(exc)[:400]}", 12, level="warn")
                frontend["note"] = f"unavailable: {str(exc)[:160]}"
        # the backend (optional second source): API routes for journey planning and load-test targets, kept out of UI coverage
        api_routes: list[BackendRoute] = []
        backend_path = None
        backend_blocked = "Provide a backend Git URL and token" if backend_required and not sources else ""
        for src in (source for source in sources if source["role"] == "backend"):
            ctx.event("ingest", f"Fetching backend codebase {src['source']}", 12)
            try:
                branch = (options.get("branch") or None) if backend_required else None
                if backend_required and not token:
                    raise ValueError("Missing Git token")
                if checkout_dir is None:
                    checkout_dir = checkouts.enter_context(repository_workspace(settings.artifacts_dir))
                brepo = fetch_repo(src["source"], checkout_dir / "backend", token=token, branch=branch)
                backend_path, src["commit_sha"] = brepo.path, brepo.commit_sha
                if backend_required:
                    commit_sha = brepo.commit_sha
                api_routes = backend_routes(brepo.path)
                src["note"] = f"{len(api_routes)} API routes ({sum(1 for r in api_routes if r.auth)} with an auth guard hint)"
                ctx.event("ingest", f"Backend: {src['note']}", 13)
            except Exception as exc:
                if backend_required:
                    backend_blocked = ("The backend source could not be checked out. Check the repository URL, branch, and token access."
                                       if backend_path is None else "The backend checkout could not be parsed for routes.")
                    src["note"] = backend_blocked
                    ctx.event("ingest", backend_blocked, 13, level="warn")
                else:
                    src["note"] = f"unavailable: {str(exc)[:160]}"
                    ctx.event("ingest", f"Backend codebase unavailable, continuing with the app's code only: {str(exc)[:300]}", 13, level="warn")
        client_eps: list[ClientEndpoint] = client_endpoints(repo_path) if repo_path else []
        api_bases = []
        if not backend_required:
            client_bases = base_urls(repo_path) if repo_path else []
            backend_bases = base_urls(backend_path) if backend_path else []
            api_bases = list(dict.fromkeys(client_bases + backend_bases))
        if client_eps or api_routes:
            ctx.event("ingest", f"API: the app calls {len(client_eps)} endpoint(s); backend declares {len(api_routes)} route(s); base URL(s) in the code: {', '.join(api_bases) or 'none'}", 13)
        (run_dir / "backend.json").write_text(json.dumps({**summarize(api_routes, client_eps), "base_urls": api_bases, "sources": sources}, indent=1), encoding="utf-8")
        backend_stats = {"routes": len(api_routes), "client_endpoints": len(client_eps), "base_urls": api_bases}
        backend_tests, backend_findings = [], []
        if backend_required:
            inventory_test = TestRecord(
                test_key="backend|route-inventory", name="Identify backend API routes", technique="backend-source", category="functional",
                route="backend", expected="Discover API route declarations in the backend repository",
                observed=backend_blocked or (f"Found {len(api_routes)} route declarations" if api_routes else "No supported route declarations found"),
                status="pass" if api_routes and not backend_blocked else "blocked",
            )
            backend_tests.append(inventory_test)
        load_config = LoadConfig.from_env(options)
        if build_only:
            load_config.enabled = False
        elif backend_required:
            load_config.enabled = bool(options.get("load_test") and options.get("load_test_base_url"))
            load_config.bearer_token = ""

        if backend_required:
            if assertions.get("api"):
                ctx.event("backend", "Checking the submitted API expectation", 14)
                try:
                    api_result = check_api_assertion(assertions["api"], secrets.get("api_token", ""))
                except Exception as exc:
                    reason = f"The submitted API check stopped ({type(exc).__name__})."
                    api_result = _blocked_check("backend|api-assertion-error", "API expectation", "api-assertion", "backend", reason)
                _append_assertion(api_result, backend_tests, backend_findings)
            try:
                load_summary, load_tests, load_findings = run_load_test(load_config, api_routes, client_eps, ctx.event, run_id, api_bases)
            except Exception as exc:
                reason = f"The API load test stopped ({type(exc).__name__})."
                ctx.event("backend", reason, 14, level="warn")
                load_summary = {"status": "blocked", "reason": reason}
                load_tests = [_blocked_check("load|error", "Backend API load test", "load", "backend", reason)]
                load_findings = []
            backend_tests += load_tests
            backend_findings += load_findings
            legs["backend"] = _leg_result(backend_tests, backend_tests)
            backend_report = {"platform": platform, "test_mode": test_mode, "code_available": False, "sources": sources,
                              "backend": backend_stats, "load_test": load_summary, "legs": legs}
            with session(settings.database_url) as db:
                r = db.get(Run, run_id)
                r.commit_sha, r.framework = commit_sha, "backend"
                db.commit()

        if test_mode == "backend":
            stats = {**backend_report, "duration_seconds": round(time.time() - t0, 1)}
            finished = _persist_results(ctx, stats, [], [], backend_tests, backend_findings, f"Backend checks {legs['backend']['status']}")
            report_path = write_report(run_dir, run_id, {"live_url": build_spec, "repo_url": repo_url, "commit_sha": commit_sha,
                                                       "finished_at": finished.isoformat(), "sources": sources}, stats, [], [], backend_tests, backend_findings)
            ctx.event("done", f"Report written: {report_path}", 100)
            return

        frontend_pending = test_mode == "full"
        intents = {el.key: app_intent(el) for el in inv.elements}
        code_available = bool(inv.files_parsed)
        (run_dir / "inventory.json").write_text(json.dumps({**inv.to_dict(), "intents": intents}, indent=1, default=str), encoding="utf-8")

        ctx.event("ingest", f"Inspecting build {build_spec}", 14)
        if uploaded_apk is not None and not uploaded_apk.is_file():
            raise FileNotFoundError("Uploaded APK is missing from the host worker's shared upload directory")
        build = resolve_build(str(uploaded_apk) if uploaded_apk is not None else build_spec, platform)
        framework = framework or build.framework
        ctx.event("ingest", f"{build.kind} {build.app_id or ''} {build.version or ''} ({build.framework or 'unknown framework'}){' sha256 ' + build.sha256[:12] if build.sha256 else ''}", 16)
        static_tests, static_findings = build_checks(build)
        t, f = code_checks(inv)
        static_tests += t
        static_findings += f
        tests, findings = static_tests, static_findings
        if static_tests:
            ctx.event("ingest", f"Build and code checks: {sum(1 for t in static_tests if t.status == 'pass')}/{len(static_tests)} passed"
                      + (f"; {len(static_findings)} finding(s): " + "; ".join(x.title for x in static_findings[:4]) if static_findings else ""), 17)
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            r.commit_sha, r.framework = commit_sha, " / ".join(part for part in (framework, platform) if part)
            db.commit()

        # 2. explore --------------------------------------------------------
        frontend_stage = f"Start the {platform_label} Appium session"
        clean_start = platform != "macos" and bool(options.get("clean_start"))
        ctx.event("explore", f"Starting Appium session on {platform}" + (" (clean start: app data cleared)" if clean_start else ""), 18)
        dev = AppDevice(settings, build, run_dir, ctx.event, clean_start=clean_start)
        dev.start()
        frontend_stage = f"Explore the {platform_label} app"
        ctx.event("explore", f"App launched in {dev.launch_seconds}s", 22)
        lc = label_classes(inv, intents)
        desktop = platform == "macos"
        default_screens = settings.macos_max_screens if desktop else settings.max_screens
        max_screens = default_screens if test_mode else max(1, int(options.get("max_pages") or default_screens))
        explorer = AppExplorer(dev, ctx.event, max_screens=max_screens, max_depth=max_screens if desktop else 3,
                               budget_seconds=settings.macos_exploration_seconds if desktop else None, label_class=lc,
                               code_menu_items={normalize(e.label) for e in inv.elements if e.kind == "menuitem" and e.label})
        explorer.skip_labels = sorted({e.label for e in inv.elements if e.label and SKIP_SETUP_RE.search(e.label)}) or ["Skip for now", "Skip", "Not now"]
        first = explorer.settled(15.0)  # past splash and loading screens
        extra_tests, extra_findings = [], []
        enabled = set(options.get("modules") or APP_MODULES) if options.get("phase2", True) else {"launch"}
        signed_in = explorer.login_fields(first)[1] is None
        if signed_in:
            first = explorer.skip_setup(first)  # a session left on an optional setup screen ("Set a PIN")
        explorer.credentials = (secrets.get("email", ""), secrets.get("password", ""))
        explorer.started_signed_out = not signed_in and not all(explorer.credentials)
        if signed_in and not all(explorer.credentials) and platform != "macos":
            ctx.event("explore", "The app is already signed in and the run has no test account: nothing that relaunches the app will run, so the device stays signed in", 24)
        if not signed_in:
            login_route = next((r.path for r in inv.routes if not r.protected), "screen:sign-in")
            if "auth" in enabled:
                t, f = signin_negative(dev, explorer, first, secrets.get("email", ""), login_route)
                extra_tests += t
                extra_findings += f
                first = dev.snapshot(0.5)
            ok, msg, first = explorer.sign_in(first, secrets.get("email", ""), secrets.get("password", ""))
            ctx.event("explore", f"Sign-in: {msg}", 24, level="info" if ok else "warn")
            if not ok and all(explorer.credentials):
                # the account, the app's server or the network refused it: test what a signed-out user can reach, never relaunch
                ctx.event("explore", "Could not sign in with the test account: continuing signed out (sign-in screen and anything reachable without an account); relaunch checks are skipped", 24, level="warn")
                explorer.credentials = ("", "")
                explorer.started_signed_out = True
                if (pwd := explorer.login_fields(first)[1]) is not None:
                    dev.type_into(pwd, "")  # leave no real password typed into the app
                    dev.hide_keyboard()
                    first = dev.snapshot(0.5)
            signed_in = ok
            if ok:
                first = explorer.skip_setup(explorer.settled(8.0))
        explorer.run(first)
        for w in explorer.warnings:
            ctx.event("explore", w, None, level="warn")
        desktop_coverage = explorer.coverage_details() if desktop else None
        coverage_result = None
        if desktop_coverage is not None:
            tapped = desktop_coverage["exercised"]
            completed_scope = desktop_coverage["complete_within_scope"]
            detail = (f"{desktop_coverage['states_walked']} views inspected, {tapped} controls exercised, "
                      f"{desktop_coverage['held']} held, {desktop_coverage['unreachable']} unreached")
            coverage_result = TestRecord(test_key="desktop|traversal", name="Explore reachable desktop controls", technique="coverage",
                                         category="functional", route="desktop", expected="Inspect the discovered app views and exercise their safe controls",
                                         observed=detail, status="pass" if completed_scope else "blocked")
            tests.append(coverage_result)
            ctx.event("explore", detail, 55, level="info" if completed_scope else "warn")
        else:
            tapped = sum(1 for t in explorer.taps if t.result not in ("skipped", "not_reached"))
            ctx.event("explore", f"Explored {len(explorer.screens)} screens; {tapped} controls tapped, {sum(1 for t in explorer.taps if t.result == 'skipped')} held back by the safety rules", 55)

        # 3. reconcile ------------------------------------------------------
        assign_routes(explorer.screens, inv, texts_by_screen)
        protected_routes = {r.path for r in inv.routes if r.protected}
        pages = to_pages(explorer.screens, build.app_id or build.label, protected_routes, signed_in, platform, explorer.taps)
        rec = reconcile(inv.elements, pages, sorted(protected_routes), "")
        components = build_components(rec, intents, code_available=code_available)
        by_screen_el = {}
        for c in components:
            if c.page_url and "#" in c.page_url and c.selector:
                by_screen_el[(c.page_url.rsplit("#", 1)[1], c.selector)] = c
        (run_dir / "explore.json").write_text(json.dumps({"screens": [{"route": s.route, "kind": s.kind, "title": s.snap.title, "activity": s.snap.activity, "path": [e.name for e in s.path],
                                                                       "elements": [asdict(e) for e in s.snap.elements], "texts": s.snap.texts[:80]} for s in explorer.screens],
                                                          "taps": [{**{k: v for k, v in asdict(t).items() if k != "element"}, "element": t.element.name, "selector": t.element.selector} for t in explorer.taps],
                                                          **({"desktop_coverage": desktop_coverage} if desktop_coverage is not None else {})},
                                                         indent=1, default=str), encoding="utf-8")
        if code_available:
            ctx.event("reconcile", f"Matched {rec.stats['matched']}/{rec.stats['code_interactive']} code controls on the device ({rec.coverage * 100:.0f}%); {rec.stats['observed_unmatched_visible']} controls on screen have no code declaration", 60)
        else:
            ctx.event("reconcile", f"{rec.stats['observed_unmatched_visible']} controls observed on the device. Source coverage unavailable.", 60)
        if assertions.get("ui"):
            ui_result = check_ui_assertion(explorer.screens, assertions["ui"])
            _append_assertion(ui_result, tests, findings)

        # 4-5. execute + judge ----------------------------------------------
        frontend_stage = f"Run the {platform_label} app checks"
        failing = [t for t in explorer.taps if t.result in ("crash", "anr", "error")]
        if failing:
            ctx.event("judge", f"Reproducing {len(failing)} failing taps from a fresh launch", 62)
            for t in failing[:15]:
                explorer.reproduce(t)
        density = 0.0
        try:
            density = float(dev.driver.capabilities.get("deviceScreenDensity") or 0)
        except Exception:
            pass
        t1, f1 = screen_checks(explorer.screens, density, platform)
        t2, f2 = tap_results(explorer, by_screen_el)
        tests += t1 + t2 + extra_tests
        findings += f1 + f2 + extra_findings
        # journeys with QA test data: planned from the code (and the LLM), validated by the safety rules, run, cleaned up
        ai_info: dict = {"used": False}
        journey_stats: dict = {"planner": "off", "total": 0, "completed": 0, "items": [], "actions": 0}
        journey_held: list[dict] = []
        if options.get("journeys", True) and options.get("phase2", True) and (platform not in SCOPED_APP_PLATFORMS or code_available):
            ai_plan = None
            if native_ai.enabled(settings, options):
                ctx.event("journey", f"Asking {settings.gemini_model} for screen intent, journeys and validation cases (no credentials are sent)", 64)
                ai_plan, err = native_ai.plan(settings, inv, explorer.screens, repo_path, api_routes, client_eps, (secrets.get("email", ""), secrets.get("password", "")))
                n = native_ai.apply_intent(ai_plan, inv, intents)
                ai_info = {"used": ai_plan is not None, "model": settings.gemini_model, "error": err[:200] if err else "", "controls_described": n,
                           "journeys_planned": len((ai_plan or {}).get("journeys", []))}
                ctx.event("journey", f"LLM plan: {ai_info['journeys_planned']} journeys, {n} control expectations" if ai_plan else f"LLM unavailable, using the plan from the code: {err[:200]}", 65,
                          level="info" if ai_plan else "warn")
            else:
                ai_info = {"used": False, "reason": "no GEMINI_API_KEY" if not settings.gemini_api_key else "LLM switched off for this run"}
            plan = plan_from_code(inv, {s.route for s in explorer.screens}) + journeys_from_ai(ai_plan)
            observed_labels = {normalize(e.name) for s in explorer.screens for e in s.snap.elements if e.name}
            validate(plan, inv, lc, repo_path, observed_labels, secrets=(secrets.get("email", ""), secrets.get("password", "")), allow_remote=bool(options.get("remote_qa_data")))
            (run_dir / "journeys.json").write_text(json.dumps(plan_dump(plan), indent=1, default=str), encoding="utf-8")
            ctx.event("journey", f"{len(plan)} journeys planned ({sum(1 for j in plan if j.source == 'ai')} by the LLM); {sum(1 for j in plan if j.blocked)} stopped early by the safety rules", 66)
            runner = JourneyRunner(dev, explorer, registry, inv, ctx.event, lc)
            results = runner.run_all(plan, budget_s=float(os.environ.get("JOURNEY_BUDGET_SECONDS") or 900))
            runner.cleanup()
            journey_held = [h for r in results for h in r.held] + [{"screen": next((s.screen for s in r.journey.steps if s.screen), ""), "label": "(journey)", "class": "guard", "reason": r.journey.blocked}
                                                                   for r in results if r.journey.blocked]
            tests += to_tests(results, registry)
            for r in results:
                findings += r.findings
            if ai_info.get("used"):
                reviews, _ = native_ai.review(settings, results, (secrets.get("email", ""), secrets.get("password", "")))
                for rv in reviews:
                    r = next((x for x in results if x.journey.name == rv.get("name")), None)
                    if r is not None and rv.get("verdict") == "suspicious":
                        route = next((s.screen for s in r.journey.steps if s.screen), "journey")
                        findings.append(FindingRecord(fingerprint=fingerprint(route, "functional", "ai review", r.journey.name), route=route, category="functional", severity="low",
                                                      title=f"LLM review: journey '{r.journey.name}' looks wrong", description="Second look by the LLM at the observed steps; confirm before acting.",
                                                      expected=r.journey.rationale or "The journey's intent", observed=str(rv.get("reason"))[:400], standard_ref="AI-assisted review (unverified)",
                                                      viewport="device", confidence=0.5, status="needs_review", evidence=list(r.evidence), steps=[f"{e['action']} {e['target']} {e.get('value', '')} → {e['result']}" for e in r.log]))
            journey_stats = journey_summary(results, "code + LLM" if ai_info.get("used") else "code")
            qa = registry.summary()
            ctx.event("journey", f"Journeys: {journey_stats['completed']}/{journey_stats['total']} completed, {journey_stats['validation_cases']} validation cases; QA data: {qa['created']} created, {qa['cleaned']} cleaned, {qa['leftover']} left over", 70,
                      level="info" if not qa["leftover"] else "warn")
        home_route = explorer.home.route if explorer.home else ""
        active = ActiveChecks(dev, explorer, ctx.event, home_route, by_screen_el=by_screen_el)
        active.run(enabled)
        tests += active.tests
        findings += active.findings

        if test_mode:
            required = [check for check in (ui_result, coverage_result) if check is not None]
            legs["frontend"] = _leg_result(tests, required)
        if not backend_required:
            # Existing load test for source-assisted native runs. Frontend testing disables it.
            load_summary, lt_tests, lt_findings = run_load_test(load_config, api_routes, client_eps, ctx.event, run_id, api_bases)
            tests += lt_tests
            findings += lt_findings
        explain(findings, inv=inv, components=components, screens=explorer.screens, build=build, signed_in=signed_in, repo=repo_path)

        # 6. report ---------------------------------------------------------
        stats = {
            **rec.stats, "coverage": round(rec.coverage, 4), "coverage_unconditional": round(rec.coverage_unconditional, 4), **coverage_after(components),
            "platform": platform, "test_mode": test_mode, "build": {k: v for k, v in asdict(build).items() if k in ("kind", "app_id", "version", "framework", "sha256", "label")},
            "code_available": code_available, "pages": len(pages), "screens": len(explorer.screens), "routes": len(inv.routes),
            "taps": tapped, "held_back": sum(1 for t in explorer.taps if t.result == "skipped" and t.action_class != "safe") + len(journey_held),
            "held_actions": held_actions(explorer) + journey_held, "safe_actions": tapped + journey_stats.get("actions", 0),
            "journeys": journey_stats, "qa_data": registry.summary(), "load_test": load_summary, "ai": ai_info, "sources": sources,
            "backend": backend_stats,
            "components_by_status": {s: sum(1 for c in components if c.status == s) for s in {c.status for c in components}},
            "duration_seconds": round(time.time() - t0, 1),
        }
        if legs:
            stats["legs"] = legs
        if desktop_coverage is not None:
            stats["desktop_coverage"] = desktop_coverage
        summary = f"{len(explorer.screens)} screens, {tapped} controls tapped"
        if backend_required:
            summary += f"; Frontend checks {legs['frontend']['status']}; Backend checks {legs['backend']['status']}"
        summary += f", {stats['coverage_interaction'] * 100:.0f}% of code controls found" if code_available else " (source coverage unavailable)"
        frontend_pending = False
        tests += backend_tests
        findings += backend_findings
        if ui_result and api_result:
            tests.append(_combined_assertion(ui_result, api_result))
        finished = _persist_results(ctx, stats, pages, components, tests, findings, summary)
        terminal = True
        report_path = write_report(run_dir, run_id, {"live_url": f"{platform}: {build.app_id or build_spec}", "repo_url": repo_url, "commit_sha": commit_sha, "framework": framework, "finished_at": finished.isoformat(),
                                                     "sources": sources},
                                   stats, pages, components, tests, findings)
        completed = True
        ctx.event("done", f"Report written: {report_path}", 100)
    except Exception as exc:
        if frontend_pending:
            reason = f"{frontend_stage} did not complete ({type(exc).__name__}). Review the host worker and Appium logs."
            ctx.event("explore", reason, None, level="warn")
            device_result = _blocked_check("frontend|device", f"{platform_label} app checks", "device", "frontend", reason)
            tests.append(device_result)
            if assertions.get("ui") and ui_result is None:
                ui_result = _blocked_check("assertion|ui", "Expected text on app screen", "ui_assertion", "frontend", reason)
                tests.append(ui_result)
            legs["frontend"] = _leg_result(tests, [device_result])
            combined = [_combined_assertion(ui_result, api_result)] if ui_result and api_result else []
            partial_tests, partial_findings = tests + backend_tests + combined, findings + backend_findings
            stats = {**backend_report, "pages": len(pages), "screens": len(pages), "duration_seconds": round(time.time() - t0, 1)}
            summary = f"Frontend checks {legs['frontend']['status']}; Backend checks {legs['backend']['status']}"
            try:
                finished = _persist_results(ctx, stats, pages, components, partial_tests, partial_findings, summary)
                report_path = write_report(run_dir, run_id, {"live_url": build_spec, "repo_url": repo_url, "commit_sha": commit_sha,
                                                           "finished_at": finished.isoformat(), "sources": sources},
                                           stats, pages, components, partial_tests, partial_findings)
                ctx.event("done", f"Report written: {report_path}", 100)
                terminal = True
                return
            except Exception as report_exc:
                exc = report_exc
        logger.exception("app run %s failed", run_id)
        with session(settings.database_url) as db:
            r = db.get(Run, run_id)
            if r is not None:
                r.status, r.finished_at, r.secrets = "failed", datetime.now(timezone.utc), None
                r.error = f"{type(exc).__name__}: {str(exc)[:900]}\n{traceback.format_exc()[-1500:]}"
                r.message = f"failed in {r.stage}: {type(exc).__name__}: {str(exc)[:200]}"
                if registry.records:
                    r.message += f"; QA data created before the failure is listed in qa_data.json ({len(registry.records)} item(s))"
                db.add(RunEvent(run_id=run_id, level="error", stage=r.stage, message=r.message))
                db.commit()
                terminal = True
    finally:
        if runner is not None and registry.pending() and dev is not None:
            try:
                runner.cleanup()  # a run that failed halfway still removes what it created where it can
            except Exception:
                logger.exception("QA data cleanup after a failure")
        if registry.pending() and not completed:
            leftover = [f"{r['id']} ({r['entity']} on {r['where']}): {r['note'] or r['status']}" for r in registry.pending()]
            try:
                ctx.event("cleanup", f"QA data left over ({len(leftover)}): " + "; ".join(leftover)[:900], None, level="warn")
            except Exception:
                pass
        try:
            if dev is not None:
                dev.stop()
        finally:
            if build is not None:
                build.close()
            checkouts.close()
            if uploaded_apk is not None and terminal:
                try:
                    uploaded_apk.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove uploaded APK for run %s", run_id, exc_info=True)
