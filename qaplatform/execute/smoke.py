"""Phase 1 execution: route guards, page health, responsive checks, and a
non-destructive smoke click on every safe interactive component.

Every test carries an expectation. Where the code gave us one (a link's
target, a handler that opens a drawer, a logout that must end the session)
that is the oracle; otherwise the oracle is "no error signal after the
click". Failures are re-run before they are reported.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeout

from qaplatform.config import Settings
from qaplatform.execute.guard import NetworkGuard, describe
from qaplatform.explore.browser import BrowserSession, attach_monitor, goto, login, same_origin
from qaplatform.explore.crawler import GuardResult, ObservedPage
from qaplatform.explore.scripts import COLLECT_INTERACTIVE_JS, PAGE_STATE_JS
from qaplatform.judge.gate import confidence, gate_status

logger = logging.getLogger(__name__)
EventCb = Callable[..., None]

CLOSE_RE = re.compile(r"^(cancel|close|dismiss|done|no|keep|go back|✕|×|x)$", re.I)
CLICKABLE_KINDS = {"button", "link", "clickable", "menuitem", "tab", "switch", "checkbox", "radio"}


def step_of(comp: "ComponentRecord") -> dict:
    return {"selector": comp.selector, "role": comp.role, "name": comp.name or comp.label, "file": comp.code_file or None}


@dataclass
class ComponentRecord:
    key: str
    route: str
    kind: str
    label: str
    source: str
    code_file: str = ""
    code_line: int | None = None
    handler: str = ""
    selector: str = ""
    role: str = ""
    name: str = ""
    href: str | None = None
    attrs: dict = field(default_factory=dict)
    intent: dict = field(default_factory=dict)
    action_class: str = "safe"
    conditional: bool = False
    page_url: str | None = None
    page_protected: bool = False
    visible: bool = False
    enabled: bool = True
    in_dialog: bool = False
    status: str = "untested"
    notes: str = ""
    reveal: list = field(default_factory=list)
    smoke_signals: dict = field(default_factory=dict)
    db_id: int | None = None


@dataclass
class TestRecord:
    test_key: str
    name: str
    technique: str
    category: str
    route: str
    steps: list = field(default_factory=list)
    expected: str = ""
    observed: str = ""
    status: str = "pass"
    duration_ms: int = 0
    reproduced_n: int = 0
    reproduced_m: int = 0
    component_key: str | None = None
    db_id: int | None = None


@dataclass
class FindingRecord:
    fingerprint: str
    route: str
    category: str
    severity: str
    title: str
    description: str = ""
    expected: str = ""
    observed: str = ""
    standard_ref: str = ""
    viewport: str = ""
    confidence: float = 0.9
    reproduced_n: int = 0
    reproduced_m: int = 0
    status: str = "reported"
    evidence: list = field(default_factory=list)
    component_key: str | None = None
    test_key: str | None = None


NAME_OF_JS = "el => (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || el.getAttribute('title') || '').trim()"


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _same_name(loc, expected: str) -> bool:
    try:
        actual = loc.evaluate(NAME_OF_JS)
    except Exception:
        return False
    a, e = _norm(actual), _norm(expected)
    if not e:
        return True
    return a == e or (len(e) >= 3 and (e in a or a in e))


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:20]


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")[:60] or "x"


def _path(url: str) -> str:
    p = urlparse(url).path or "/"
    return p[:-1] if len(p) > 1 and p.endswith("/") else p


# ---------------------------------------------------------------------------
# Browser-free checks from the crawl data
# ---------------------------------------------------------------------------

def guard_tests(guards: list[GuardResult]) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests, findings = [], []
    for g in guards:
        ok = g.redirected_to_login or g.shows_login_form
        t = TestRecord(
            test_key=f"guard|{g.route}", name=f"Protected route {g.route} requires a session", technique="route-guard", category="security", route=g.route,
            steps=[{"type": "NAVIGATE", "value": g.url, "description": "open without a session"}, {"type": "EXPECT_URL_CONTAINS", "value": "/login"}],
            expected="Unauthenticated request is redirected to /login (ProtectedRoute in code)",
            observed=f"final URL {g.final_url}" + (" (login form shown)" if g.shows_login_form else ""),
            status="pass" if ok else "fail", reproduced_n=1, reproduced_m=1,
        )
        tests.append(t)
        if not ok:
            findings.append(FindingRecord(
                fingerprint=fingerprint("guard", g.route), route=g.route, category="security", severity="critical",
                title=f"Protected route {g.route} is reachable without a session",
                description="The code wraps this route in a ProtectedRoute that redirects to /login, but the live app served it to an unauthenticated browser.",
                expected=t.expected, observed=t.observed, standard_ref="OWASP WSTG-ATHZ-02", confidence=0.95, reproduced_n=1, reproduced_m=1, test_key=t.test_key,
            ))
    return tests, findings


def page_tests(pages: list[ObservedPage], settings: Settings) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests, findings = [], []
    for p in pages:
        route = p.route
        shot = {"type": "screenshot", "path": p.screenshot} if p.screenshot else None
        # 1. document status / blank page
        blank = bool(p.state.get("rootEmpty")) or int(p.state.get("bodyTextLength", 0)) < 30
        bad_status = p.status_code is not None and p.status_code >= 400
        t = TestRecord(test_key=f"load|{route}", name=f"{route} renders", technique="page-load", category="stability", route=route,
                       steps=[{"type": "NAVIGATE", "value": p.url}], expected="Document returns < 400 and renders visible content",
                       observed=f"status {p.status_code}, {p.state.get('bodyTextLength', 0)} chars of text, {len(p.elements)} controls, {p.load_ms} ms",
                       status="fail" if (blank or bad_status) else "pass", reproduced_n=1, reproduced_m=1)
        tests.append(t)
        if bad_status:
            findings.append(FindingRecord(fingerprint=fingerprint("status", route), route=route, category="stability", severity="high",
                                          title=f"{route} returned HTTP {p.status_code}", expected=t.expected, observed=t.observed,
                                          standard_ref="ISO/IEC 25010 Reliability", evidence=[e for e in [shot] if e], test_key=t.test_key))
        elif blank:
            findings.append(FindingRecord(fingerprint=fingerprint("blank", route), route=route, category="stability", severity="critical",
                                          title=f"{route} renders an empty page", expected=t.expected, observed=t.observed,
                                          standard_ref="ISO/IEC 25010 Reliability", evidence=[e for e in [shot] if e], test_key=t.test_key))
        # 2. console / page errors
        errs = list(dict.fromkeys(p.page_errors + p.console_errors))
        t2 = TestRecord(test_key=f"console|{route}", name=f"{route} loads without console errors", technique="page-load", category="stability", route=route,
                        steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "OBSERVE", "value": "console"}],
                        expected="No uncaught exceptions or console.error output during load",
                        observed=f"{len(errs)} error(s)" + (": " + errs[0][:160] if errs else ""), status="fail" if errs else "pass", reproduced_n=1, reproduced_m=1)
        tests.append(t2)
        if errs:
            findings.append(FindingRecord(fingerprint=fingerprint("console", route), route=route, category="stability", severity="medium",
                                          title=f"Console errors while loading {route}", description="\n".join(errs[:5]), expected=t2.expected, observed=t2.observed,
                                          standard_ref="ISO/IEC 25010 Reliability", evidence=[{"type": "console", "messages": errs[:10]}] + ([shot] if shot else []), test_key=t2.test_key))
        # 3. failed requests
        failed = p.failed_requests
        server = [r for r in failed if (r.get("status") or 0) >= 500]
        network = [r for r in failed if r.get("status") is None]
        client = [r for r in failed if r.get("status") and 400 <= r["status"] < 500]
        t3 = TestRecord(test_key=f"requests|{route}", name=f"{route} loads without failed requests", technique="page-load", category="stability", route=route,
                        steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "OBSERVE", "value": "network"}],
                        expected="Every request made during load succeeds",
                        observed=f"{len(server)} server errors, {len(client)} client errors, {len(network)} network failures", status="fail" if failed else "pass", reproduced_n=1, reproduced_m=1)
        tests.append(t3)
        if server:
            findings.append(FindingRecord(fingerprint=fingerprint("5xx", route), route=route, category="stability", severity="high",
                                          title=f"Server error during load of {route}", description="\n".join(f"{r['method']} {r['url']} -> {r['status']}" for r in server[:5]),
                                          expected=t3.expected, observed=t3.observed, standard_ref="ISO/IEC 25010 Reliability", evidence=[{"type": "requests", "items": server[:10]}], test_key=t3.test_key))
        if client:
            sev = "medium" if any(r["status"] not in (401, 403) for r in client) else "low"
            findings.append(FindingRecord(fingerprint=fingerprint("4xx", route), route=route, category="stability", severity=sev,
                                          title=f"Failed requests during load of {route}", description="\n".join(f"{r['method']} {r['url']} -> {r['status']}" for r in client[:5]),
                                          expected=t3.expected, observed=t3.observed, standard_ref="ISO/IEC 25010 Reliability", evidence=[{"type": "requests", "items": client[:10]}], test_key=t3.test_key))
        if network:
            findings.append(FindingRecord(fingerprint=fingerprint("net", route), route=route, category="stability", severity="medium",
                                          title=f"Network failures during load of {route}", description="\n".join(f"{r['method']} {r['url']} {r['error']}" for r in network[:5]),
                                          expected=t3.expected, observed=t3.observed, standard_ref="ISO/IEC 25010 Reliability", evidence=[{"type": "requests", "items": network[:10]}], test_key=t3.test_key))
        # 4. slow load
        if p.load_ms and p.load_ms > 5000:
            findings.append(FindingRecord(fingerprint=fingerprint("slow", route), route=route, category="performance", severity="low",
                                          title=f"{route} took {p.load_ms} ms to load", expected="Page settles within 5 s", observed=f"{p.load_ms} ms", standard_ref="Core Web Vitals", test_key=t.test_key))
        # 5. responsive
        for vname, layout in p.viewports.items():
            if "error" in layout:
                continue
            vshot = {"type": "screenshot", "path": layout.get("screenshot", ""), "viewport": vname}
            issues = []
            if layout.get("horizontalOverflow"):
                issues.append("horizontal overflow")
                findings.append(FindingRecord(fingerprint=fingerprint("overflow", route, vname), route=route, category="ui-responsive", severity="medium",
                                              title=f"Horizontal overflow on {route} at {vname} ({layout['width']}px)",
                                              description="The page is wider than the viewport, so content requires sideways scrolling. Offending elements: " + "; ".join(f"<{d['tag']}{('#' + d['id']) if d.get('id') else ''}> {d['text'][:40]!r} {d['bbox']}" for d in layout.get("overflowing", [])[:5]),
                                              expected=f"Content reflows within {layout['width']}px without a horizontal scrollbar", observed=f"scrollWidth {layout['scrollW']} > viewport {layout['docW']}",
                                              standard_ref="WCAG 2.2 1.4.10 Reflow", viewport=vname, evidence=[vshot, {"type": "layout", "overflowing": layout.get("overflowing", [])[:10]}]))
            if layout.get("overlaps"):
                issues.append("overlapping controls")
                findings.append(FindingRecord(fingerprint=fingerprint("overlap", route, vname), route=route, category="ui-responsive", severity="medium",
                                              title=f"Overlapping controls on {route} at {vname}",
                                              description="; ".join(f"{o['a']['tag']} {o['a']['text'][:30]!r} overlaps {o['b']['tag']} {o['b']['text'][:30]!r}" for o in layout["overlaps"][:5]),
                                              expected="Interactive controls do not overlap each other", observed=f"{len(layout['overlaps'])} overlapping pair(s)",
                                              standard_ref="Nielsen #4 Consistency and standards", viewport=vname, evidence=[vshot, {"type": "layout", "overlaps": layout["overlaps"][:10]}]))
            if layout.get("smallTargets"):
                issues.append("small targets")
                findings.append(FindingRecord(fingerprint=fingerprint("target", route, vname), route=route, category="accessibility", severity="low",
                                              title=f"Tap targets under 24px on {route} at {vname}",
                                              description="; ".join(f"<{d['tag']}> {d['text'][:30]!r} {d['bbox'][2]}x{d['bbox'][3]}px" for d in layout["smallTargets"][:6]),
                                              expected="Interactive targets are at least 24 by 24 CSS pixels", observed=f"{len(layout['smallTargets'])} small target(s)",
                                              standard_ref="WCAG 2.2 2.5.8 Target Size (Minimum)", viewport=vname, evidence=[vshot, {"type": "layout", "smallTargets": layout["smallTargets"][:10]}]))
            if layout.get("clipped"):
                issues.append("clipped text")
                findings.append(FindingRecord(fingerprint=fingerprint("clip", route, vname), route=route, category="ui-responsive", severity="low",
                                              title=f"Clipped text on {route} at {vname}",
                                              description="; ".join(f"<{d['tag']}> {d['text'][:40]!r}" for d in layout["clipped"][:6]),
                                              expected="Text is fully visible or truncated with an ellipsis", observed=f"{len(layout['clipped'])} element(s) cut off",
                                              standard_ref="WCAG 2.2 1.4.10 Reflow", viewport=vname, evidence=[vshot, {"type": "layout", "clipped": layout["clipped"][:10]}]))
            tests.append(TestRecord(test_key=f"responsive|{route}|{vname}", name=f"{route} layout at {vname} ({layout['width']}px)", technique="responsive", category="ui-responsive", route=route,
                                    steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "SET_VIEWPORT", "extra": {"width": layout["width"], "height": layout["height"]}}, {"type": "OBSERVE", "value": "layout"}],
                                    expected="No overflow, overlap, clipped text or undersized targets", observed=", ".join(issues) or "clean", status="fail" if issues else "pass", reproduced_n=1, reproduced_m=1))
    return tests, findings


# ---------------------------------------------------------------------------
# Smoke clicks in the browser
# ---------------------------------------------------------------------------

class SmokeRunner:
    """Signed-in browser under the write guard, plus the click-and-judge routine every module shares."""

    def __init__(self, settings: Settings, live_url: str, storage_state_path: str, run_dir: Path, secrets: dict, events: EventCb, allow_mutations: bool = False):
        self.settings = settings
        self.live_url = live_url.rstrip("/")
        self.storage_state = storage_state_path
        self.run_dir = run_dir
        self.shots = run_dir / "screenshots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.secrets = secrets
        self.events = events
        self.allow_mutations = allow_mutations
        self._dirty = False
        self.ctx = None
        self.page = None
        self.mon = None
        self.guard: NetworkGuard | None = None

    # -- session -----------------------------------------------------------
    @contextlib.contextmanager
    def session(self, clock: bool = False):
        """A fresh signed-in context with the write guard installed. Optionally with a controllable clock."""
        with BrowserSession(self.settings) as bs:
            ctx = bs.new_context()
            guard = NetworkGuard(ctx).install()
            self.ctx, self.guard = ctx, guard
            ok = self._relogin(ctx) if self.secrets.get("email") else False
            if not ok and self.storage_state and Path(self.storage_state).exists():
                ctx.close()
                ctx = bs.new_context(storage_state=self.storage_state)
                guard = NetworkGuard(ctx).install()
                self.ctx, self.guard = ctx, guard
            if clock:
                ctx.clock.install()
            self.page = ctx.new_page()
            self.mon = attach_monitor(self.page)
            self._dirty = True
            try:
                yield self
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
                self.ctx = self.page = self.mon = self.guard = None

    def new_page(self) -> None:
        with contextlib.suppress(Exception):
            self.page.close()
        self.page = self.ctx.new_page()
        self.mon = attach_monitor(self.page)
        self._dirty = True

    def _relogin(self, ctx) -> bool:
        cm = self.guard.suspended() if self.guard else contextlib.nullcontext()
        with cm:
            res = login(ctx, self.live_url, self.secrets.get("email", ""), self.secrets.get("password", ""), self.settings, self.run_dir, save_state=False)
        return res.success

    # -- helpers -----------------------------------------------------------
    def state(self) -> dict:
        try:
            return self.page.evaluate(PAGE_STATE_JS)
        except Exception:
            return {"path": _path(self.page.url), "dialogs": 0, "menus": 0, "overlays": 0, "domHash": 0, "bodyTextLength": 0, "rootEmpty": False}

    def collect(self) -> list[dict]:
        try:
            return self.page.evaluate(COLLECT_INTERACTIVE_JS, self.settings.max_elements_per_page)
        except Exception:
            return []

    def locate(self, selector: str | None, role: str | None, name: str | None):
        """Selector first, but only if the element it finds carries the expected name; else role + name."""
        page = self.page
        loc = None
        if selector:
            loc = page.locator(selector).first
            try:
                if loc.count() == 0:
                    loc = None
                elif name and not _same_name(loc, name):
                    loc = None
            except Exception:
                loc = None
        if loc is None and role and name:
            loc = page.get_by_role(role, name=name, exact=True).first
            try:
                if loc.count() == 0:
                    loc = page.get_by_role(role, name=re.compile(re.escape(name), re.I)).first
                    if loc.count() == 0:
                        loc = None
            except Exception:
                loc = None
        return loc

    def prepare(self, page_url: str, reveal: list | None = None) -> tuple[bool, str]:
        """Fresh load of page_url, then replay the clicks that reveal a component."""
        goto(self.page, page_url, self.settings)
        self._dirty = False
        protected_target = "/login" not in _path(page_url)
        if protected_target and "/login" in _path(self.page.url):
            if not (self._relogin(self.ctx) and (goto(self.page, page_url, self.settings) or True) and "/login" not in _path(self.page.url)):
                return False, "page redirected to login"
        for i, step in enumerate(reveal or []):
            loc = self.locate(step.get("selector"), step.get("role"), step.get("name"))
            if loc is None:
                return False, f"reveal step {i + 1} ('{step.get('name')}') not found"
            try:
                loc.click(timeout=self.settings.action_timeout_ms)
            except Exception as exc:
                return False, f"reveal step {i + 1} ('{step.get('name')}') not clickable: {type(exc).__name__}"
            self.page.wait_for_timeout(self.settings.settle_ms)
        return True, ""

    def screenshot(self, name: str) -> dict | None:
        shot = self.shots / f"{_slug(name)}_{int(time.time() * 1000) % 100000}.png"
        try:
            self.page.screenshot(path=str(shot))
            return {"type": "screenshot", "path": str(shot)}
        except Exception:
            return None

    # -- one click, judged ---------------------------------------------------
    def click_once(self, comp: ComponentRecord, page_url: str, protected: bool, collect_after: bool = False) -> dict:
        """One click attempt. Returns a dict of signals; never raises."""
        out = {"status": "pass", "observed": "", "signals": {}, "severity": "", "title": "", "expected": "", "evidence": [], "category": "functional", "ref": "", "writes": []}
        st = self.state()
        if comp.reveal or self._dirty or _path(self.page.url) != _path(page_url) or st.get("dialogs", 0) or st.get("menus", 0) or st.get("overlays", 0):
            ok, why = self.prepare(page_url, comp.reveal)
            if not ok:
                out["status"], out["observed"] = "blocked", why
                return out
        page, mon = self.page, self.mon
        loc = self.locate(comp.selector, comp.role, comp.name or comp.label)
        if loc is None:
            out["status"], out["observed"] = "blocked", "element not found at test time"
            return out
        try:
            if not loc.is_visible(timeout=1500):
                out["status"], out["observed"] = "blocked", "element not visible at test time"
                return out
        except Exception:
            out["status"], out["observed"] = "blocked", "element not visible at test time"
            return out
        if collect_after:
            out["before_elements"] = self.collect()
        mon.reset()
        before = self.state()
        gmark = self.guard.mark() if self.guard else 0
        popups: list = []
        handler = lambda p: popups.append(p)  # noqa: E731
        self.ctx.on("page", handler)
        clicked = True
        try:
            loc.click(timeout=self.settings.action_timeout_ms)
        except PWTimeout:
            clicked = False
        except Exception as exc:
            clicked = False
            out["observed"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        if not mon.crashed:
            with contextlib.suppress(Exception):
                page.wait_for_timeout(self.settings.settle_ms)
        with contextlib.suppress(Exception):
            self.ctx.remove_listener("page", handler)
        crashed = mon.crashed
        after = self.state() if not crashed else {"path": before.get("path"), "rootEmpty": True, "bodyTextLength": 0}
        writes = self.guard.since(gmark) if self.guard else []
        out["writes"] = [f"{w.method} {w.path}" for w in writes]
        if collect_after and not crashed:
            out["after_elements"] = self.collect()
        snap = mon.snapshot()
        errors = snap["page_errors"] + snap["console_errors"]
        failed = snap["failed_requests"]
        server = [r for r in failed if (r.get("status") or 0) >= 500]
        network = [r for r in failed if r.get("status") is None]
        unauthorized = [r for r in failed if r.get("status") in (401, 403)]
        client = [r for r in failed if r.get("status") and 400 <= r["status"] < 500 and r["status"] not in (401, 403)]
        url_changed = after.get("path") != before.get("path")
        layers_before = before.get("dialogs", 0) + before.get("menus", 0) + before.get("overlays", 0)
        layers_after = after.get("dialogs", 0) + after.get("menus", 0) + after.get("overlays", 0)
        opened = layers_after > layers_before
        changed = url_changed or opened or after.get("domHash") != before.get("domHash")
        blank = bool(after.get("rootEmpty")) or int(after.get("bodyTextLength", 0)) < 30
        to_login = "/login" in str(after.get("path", "")) and protected
        label = comp.label or comp.name
        parts = []
        if url_changed:
            parts.append(f"navigated to {after.get('path')}")
        if opened:
            parts.append("opened a dialog/menu")
        if layers_after < layers_before:
            parts.append("closed a dialog/menu")
        if popups:
            parts.append(f"opened {len(popups)} new tab(s)")
        if not changed and not popups:
            parts.append("no visible change")
        if writes:
            parts.append(f"write intercepted, not sent: {describe(writes)}")
        if snap["dialogs"]:
            parts.append(f"native {snap['dialogs'][0]['type']} dialog: {snap['dialogs'][0]['message'][:60]!r}")
        if errors and not writes:
            parts.append(f"{len(errors)} console/page error(s)")
        if failed and not writes:
            parts.append(f"{len(failed)} failed request(s)")
        out["observed"] = (out["observed"] + "; " if out["observed"] else "") + ", ".join(parts)

        intent = comp.intent or {}
        expected_paths = [t for t in (intent.get("navigates_to") or []) if isinstance(t, str) and t.startswith("/")]
        external = None
        if comp.href and not expected_paths:
            if same_origin(comp.href, self.live_url):
                expected_paths = [_path(comp.href)]
            elif comp.href.startswith("http"):
                external = comp.href
        closing = bool(CLOSE_RE.match((label or "").strip())) and layers_before > 0 and not expected_paths
        if crashed:
            out.update(status="fail", severity="critical", category="stability", ref="ISO/IEC 25010 Reliability",
                       title=f"Clicking '{label}' on {comp.route} crashes the page", expected="The page keeps running after the click")
        elif not clicked:
            out.update(status="fail", severity="medium", category="functional", ref="Nielsen #1 Visibility of system status",
                       title=f"'{label}' on {comp.route} cannot be clicked", expected="The control accepts a click")
        elif blank:
            out.update(status="fail", severity="critical", category="stability", ref="ISO/IEC 25010 Reliability",
                       title=f"Clicking '{label}' on {comp.route} blanks the page", expected="The page keeps rendering after the click")
        elif to_login and not intent.get("session_ending"):
            out.update(status="fail", severity="high", category="functional", ref="OWASP WSTG-SESS-01",
                       title=f"Clicking '{label}' on {comp.route} drops the session", expected="An authenticated user stays signed in")
        elif expected_paths:
            want = expected_paths[0].rstrip("/") or "/"
            got = str(after.get("path", "")).rstrip("/") or "/"
            out["expected"] = f"Navigates to {want}"
            if not (got == want or got.startswith(want + "/")):
                out.update(status="fail", severity="high", category="functional", ref="ISTQB state transition",
                           title=f"'{label}' on {comp.route} does not navigate to {want}", expected=f"Navigates to {want} (from the code's route/handler)")
        elif external:
            out["expected"] = f"Opens {external}"
            if not popups and not url_changed:
                out.update(status="fail", severity="medium", category="functional", ref="Nielsen #1 Visibility of system status",
                           title=f"External link '{label}' on {comp.route} does nothing")
        elif closing:
            out["expected"] = "Closes the open dialog, drawer or menu"
            if layers_after >= layers_before:
                out.update(status="fail", severity="medium", category="functional", ref="Nielsen #3 User control and freedom",
                           title=f"'{label}' on {comp.route} does not close the dialog")
        elif intent.get("opens"):
            out["expected"] = "Opens a " + "/".join(intent["opens"])
            if not (opened or changed):
                out.update(status="fail", severity="medium", category="functional", ref="Nielsen #1 Visibility of system status",
                           title=f"'{label}' on {comp.route} opens nothing")
        else:
            out["expected"] = "Responds without errors"

        # Errors after a click count only when no write was intercepted: a blocked save
        # legitimately produces a failed request and usually an error message.
        if out["status"] == "pass" and not writes and (errors or server or network or client):
            out["status"] = "fail"
            out["category"] = "stability"
            out["ref"] = "ISO/IEC 25010 Reliability"
            if server or network:
                out["severity"], out["title"] = "high", f"Request failure after clicking '{label}' on {comp.route}"
            elif errors:
                out["severity"], out["title"] = "medium", f"Console error after clicking '{label}' on {comp.route}"
            else:
                out["severity"], out["title"] = "medium", f"Failed request after clicking '{label}' on {comp.route}"
        elif out["status"] == "pass" and not writes and unauthorized:
            out.update(status="fail", severity="medium", category="security", ref="OWASP WSTG-ATHZ-02",
                       title=f"Unauthorized API response after clicking '{label}' on {comp.route}",
                       expected="An admin session is accepted by every endpoint the page calls")

        if out["status"] == "fail":
            shot = self.screenshot(f"click_{comp.route}_{label}")
            if shot:
                out["evidence"].append(shot)
            if errors:
                out["evidence"].append({"type": "console", "messages": errors[:10]})
            if failed:
                out["evidence"].append({"type": "requests", "items": failed[:10]})
        out["signals"] = {"errors": errors[:5], "failed": failed[:5], "url_after": after.get("path"), "opened": opened, "popups": len(popups), "changed": changed, "url_changed": url_changed, "writes": out["writes"]}
        out["crashed"] = crashed

        for p in popups:
            with contextlib.suppress(Exception):
                p.close()
        if crashed:
            self.new_page()
            return out
        with contextlib.suppress(Exception):
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
        if to_login and not intent.get("session_ending"):
            self._relogin(self.ctx)
            self._dirty = True
        elif url_changed or blank or opened or changed or writes or comp.reveal:
            self._dirty = True  # anything the click changed must not leak into the next click
        return out

    def test_component(self, comp: ComponentRecord, protected: bool, collect_after: bool = False, technique: str = "smoke-click") -> tuple[TestRecord, FindingRecord | None, dict]:
        """Click, judge, re-run twice on failure, and turn the outcome into a test (and maybe a finding)."""
        t0 = time.time()
        page_url = comp.page_url or self.live_url
        res = self.click_once(comp, page_url, protected, collect_after=collect_after)
        label = comp.label or comp.name
        steps = [{"type": "NAVIGATE", "value": page_url}] + [{"type": "CLICK", "target": s, "description": "reveal"} for s in comp.reveal] + [{"type": "CLICK", "target": step_of(comp)}, {"type": "OBSERVE"}]
        test = TestRecord(test_key=f"{technique}|{comp.key}", name=f"Click '{label}' on {comp.route}" + (f" (after {' > '.join(s.get('name') or '?' for s in comp.reveal)})" if comp.reveal else ""),
                          technique=technique, category=res["category"], route=comp.route, steps=steps, expected=res["expected"], observed=res["observed"],
                          status=res["status"], component_key=comp.key)
        finding = None
        comp.smoke_signals = res.get("signals", {})
        if res["writes"]:
            comp.notes = (comp.notes + "; " if comp.notes else "") + f"click sends a write ({', '.join(res['writes'][:2])}); intercepted"
            if comp.action_class == "safe":
                comp.action_class = "mutating"
        if res["status"] == "fail":
            n, m = 1, 1
            for _ in range(2):
                m += 1
                self._dirty = True
                again = self.click_once(comp, page_url, protected)
                if again["status"] == "fail":
                    n += 1
            test.reproduced_n, test.reproduced_m = n, m
            conf = confidence(n, m)
            if n < 2:
                test.status = "flaky"
            desc = f"Component: {comp.kind} '{label}' ({comp.selector or comp.role})."
            if comp.code_file:
                desc += f" Code: {comp.code_file}:{comp.code_line}."
            if comp.reveal:
                desc += " Reached by clicking: " + " > ".join(s.get("name") or "?" for s in comp.reveal) + "."
            finding = FindingRecord(fingerprint=fingerprint("click", comp.route, comp.key, res["title"]), route=comp.route, category=res["category"], severity=res["severity"],
                                    title=res["title"], description=desc, expected=res["expected"], observed=res["observed"], standard_ref=res["ref"], confidence=conf,
                                    reproduced_n=n, reproduced_m=m, status=gate_status(conf), evidence=res["evidence"], component_key=comp.key, test_key=test.test_key)
            comp.status = "fail" if n >= 2 else "flaky"
        elif res["status"] == "blocked":
            test.reproduced_n, test.reproduced_m = 0, 1
            comp.status = "blocked"
            comp.notes = res["observed"]
        else:
            test.reproduced_n, test.reproduced_m = 1, 1
            comp.status = "pass"
        test.duration_ms = int((time.time() - t0) * 1000)
        return test, finding, res

    # -- phase 1 smoke ----------------------------------------------------------
    def classify_skips(self, components: list[ComponentRecord]) -> None:
        for c in components:
            if c.status != "untested":
                continue
            if c.action_class == "destructive":
                c.status, c.notes = "skipped", "destructive control: only its confirmation dialog is opened, never confirmed"
            elif c.action_class == "download":
                c.status, c.notes = "skipped", "download link: not clicked"
            elif c.kind in ("input", "textarea", "select", "file") and c.page_url:
                c.status, c.notes = "observed", "form control: present on page"
            elif c.page_url and (not c.enabled or not c.visible or c.in_dialog):
                c.status = "observed"
                c.notes = "present but " + ("disabled" if not c.enabled else "not visible" if not c.visible else "inside a dialog") + " at rest; not clicked"

    def clickable(self, c: ComponentRecord) -> bool:
        # Mutating controls are clicked too: the guard intercepts their writes in the browser.
        return bool(c.page_url and (c.selector or (c.role and c.name)) and c.visible and c.enabled and c.kind in CLICKABLE_KINDS
                    and c.action_class in ("safe", "mutating"))

    def run(self, components: list[ComponentRecord], pages: list[ObservedPage], protected_routes: list[str]) -> tuple[list[TestRecord], list[FindingRecord]]:
        tests: list[TestRecord] = []
        findings: list[FindingRecord] = []
        page_by_url = {p.url: p for p in pages}
        todo = [c for c in components if self.clickable(c) and not c.in_dialog and not c.reveal]
        session_enders = [c for c in components if c.action_class == "session_ending" and c.page_url and c.visible and (c.selector or c.role)]
        self.classify_skips(components)
        with self.session():
            total = len(todo)
            for done, comp in enumerate(todo, 1):
                if done % 5 == 0 or done == total:
                    self.events("execute", f"Smoke-clicking {done}/{total}: {comp.label or comp.name} on {comp.route}", 60 + int(12 * done / max(total, 1)))
                observed_page = page_by_url.get(comp.page_url)
                protected = bool(observed_page and (observed_page.protected or observed_page.route in protected_routes))
                test, finding, _ = self.test_component(comp, protected)
                tests.append(test)
                if finding:
                    findings.append(finding)
            for comp in session_enders[:1]:
                t, f = self.logout_test(comp, protected_routes)
                tests.append(t)
                findings.extend(f)
        return tests, findings

    def logout_test(self, comp: ComponentRecord, protected_routes: list[str]) -> tuple[TestRecord, list[FindingRecord]]:
        """Real logout (the guard lets it through): the session must end and protected routes must redirect."""
        self.events("execute", f"Testing session end via '{comp.label or comp.name}'", 73)
        findings: list[FindingRecord] = []
        t0 = time.time()
        with self.guard.suspended():
            ok, why = self.prepare(comp.page_url)
            loc = self.locate(comp.selector, comp.role, comp.name or comp.label) if ok else None
            if loc is None:
                comp.status = "blocked"
                return TestRecord(test_key=f"logout|{comp.key}", name="Log out ends the session", technique="session", category="security", route=comp.route,
                                  expected="Logout returns to /login", observed=why or "logout control not found", status="blocked", component_key=comp.key), findings
            try:
                loc.click(timeout=self.settings.action_timeout_ms)
                self.page.wait_for_timeout(self.settings.settle_ms * 2)
            except Exception as exc:
                comp.status, comp.notes = "blocked", f"logout click failed: {type(exc).__name__}"
                return TestRecord(test_key=f"logout|{comp.key}", name="Log out ends the session", technique="session", category="security", route=comp.route,
                                  expected="Logout returns to /login", observed=comp.notes, status="blocked", component_key=comp.key), findings
            after_logout = _path(self.page.url)
            probe = next((r for r in protected_routes if r not in ("(layout)",)), None)
            probe_path, storage_cleared = None, None
            if probe:
                goto(self.page, self.live_url + probe, self.settings)
                probe_path = _path(self.page.url)
                with contextlib.suppress(Exception):
                    storage_cleared = self.page.evaluate("() => Object.keys(localStorage).filter(k => /token|session|auth/i.test(k)).length === 0")
        ok = "/login" in after_logout and (probe_path is None or "/login" in probe_path)
        test = TestRecord(test_key=f"logout|{comp.key}", name="Log out ends the session", technique="session", category="security", route=comp.route,
                          steps=[{"type": "NAVIGATE", "value": comp.page_url}, {"type": "CLICK", "target": step_of(comp)}, {"type": "EXPECT_URL_CONTAINS", "value": "/login"}, {"type": "NAVIGATE", "value": self.live_url + (probe or "")}, {"type": "EXPECT_URL_CONTAINS", "value": "/login"}],
                          expected="After logout the app shows /login and a protected route redirects to /login",
                          observed=f"after logout: {after_logout}; protected probe {probe}: {probe_path}; auth keys cleared: {storage_cleared}",
                          status="pass" if ok else "fail", duration_ms=int((time.time() - t0) * 1000), reproduced_n=1, reproduced_m=1, component_key=comp.key)
        comp.status = "pass" if ok else "fail"
        if not ok:
            findings.append(FindingRecord(fingerprint=fingerprint("logout", comp.route), route=comp.route, category="security", severity="critical",
                                          title="Logging out does not end the session", expected=test.expected, observed=test.observed,
                                          standard_ref="OWASP WSTG-SESS-06", confidence=0.9, reproduced_n=1, reproduced_m=1, component_key=comp.key, test_key=test.test_key))
        self._relogin(self.ctx)
        self._dirty = True
        return test, findings
