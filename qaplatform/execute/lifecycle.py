"""Timers from the code, checked with a controlled clock.

For every setInterval the code declares (with its period), on a page that runs it:

  period      a request is made within the period, and again in the next one
  stop        a control labelled like "Auto-refresh" stops the polling when off
  cleanup     leaving the page (in-app navigation) stops its polling

and for any timer whose callback signs the user out (idle / session timeout),
the app is on /login once the period has passed.
"""
from __future__ import annotations

import contextlib
import re
from urllib.parse import urlparse

from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint
from qaplatform.explore.crawler import ObservedPage
from qaplatform.ingest.js_parser import CodeInventory
from qaplatform.judge.gate import gate_status

TOGGLE_RE = re.compile(r"auto.?refresh|auto.?update|live.?update|polling|real.?time|auto.?reload", re.I)
LOGOUT_RE = re.compile(r"logout|signOut|sign_out|endSession|expire", re.I)


def _api_paths(reqs: list[dict]) -> list[str]:
    return [r["path"] for r in reqs if r.get("method", "GET") == "GET"]


def timer_routes(inv: CodeInventory, pages: list[ObservedPage]) -> list[tuple[object, str]]:
    """(timer, route) pairs: the route whose page or layout renders the timer's file."""
    out = []
    protected = [p.route for p in pages if p.protected and "/login" not in p.route]
    for t in inv.timers:
        if t.kind != "setInterval" or not t.ms or t.ms < 2000:
            continue
        routes = sorted({e.route for e in inv.elements if e.file == t.file and not e.shared})
        page_routes = [r for r, f in inv.page_files.items() if f == t.file]
        routes = page_routes or routes
        if routes == ["(layout)"] or (not routes and protected):
            # a layout timer runs everywhere; test it on a page without its own timers
            busy = {r for tt in inv.timers for r, f in inv.page_files.items() if f == tt.file}
            routes = [next((r for r in protected if r not in busy), protected[0] if protected else None)]
        for r in routes:
            if r and r != "(layout)" and any(p.route == r for p in pages):
                out.append((t, r))
    return out


def run_lifecycle(runner: SmokeRunner, inv: CodeInventory, pages: list[ObservedPage], components: list[ComponentRecord], events) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    pairs = timer_routes(inv, pages)
    if not pairs:
        return tests, findings
    page_by_route = {p.route: p for p in pages}
    nav_links = [c for c in components if c.route == "(layout)" and c.kind == "link" and c.href and c.visible]

    for timer, route in pairs:
        ms = int(timer.ms)
        p = page_by_route[route]
        code_ref = f"{timer.file}:{timer.line} setInterval every {ms / 1000:g}s"
        events("lifecycle", f"Clock-controlled check of {code_ref} on {route}", 88)
        with runner.session(clock=True):
            page, mon, clock = runner.page, runner.mon, runner.ctx.clock
            ok, why = runner.prepare(p.url)
            if not ok:
                tests.append(TestRecord(test_key=f"timer|{timer.file}:{timer.line}|{route}", name=f"{route} polls every {ms / 1000:g}s", technique="lifecycle", category="lifecycle",
                                        route=route, expected=code_ref, observed=why, status="blocked"))
                continue
            now = page.evaluate("Date.now()")
            clock.pause_at(now + 500)
            mon.reset()
            clock.run_for(ms - 1500)
            page.wait_for_timeout(300)
            early = _api_paths(mon.api_requests)
            mon.reset()
            clock.run_for(3000)
            page.wait_for_timeout(800)
            first = _api_paths(mon.api_requests)
            mon.reset()
            clock.run_for(ms)
            page.wait_for_timeout(800)
            second = _api_paths(mon.api_requests)
            polled = sorted(set(first) & set(second)) or sorted(set(first))
            ok_period = bool(first) and bool(second)
            t = TestRecord(test_key=f"timer|{timer.file}:{timer.line}|{route}", name=f"{route} refreshes every {ms / 1000:g}s as coded", technique="lifecycle", category="lifecycle", route=route,
                           steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "CLOCK", "value": f"pause, run {ms - 1500} ms, run 3000 ms, run {ms} ms"}],
                           expected=f"A request near {ms / 1000:g}s and again one period later ({code_ref})",
                           observed=f"before {ms / 1000 - 1.5:g}s: {len(early)} request(s); around {ms / 1000:g}s: {', '.join(sorted(set(first))) or 'none'}; next period: {', '.join(sorted(set(second))) or 'none'}",
                           status="pass" if ok_period else "fail", reproduced_n=1, reproduced_m=1)
            tests.append(t)
            if not ok_period:
                findings.append(FindingRecord(fingerprint=fingerprint("timer", route, timer.file, str(timer.line)), route=route, category="lifecycle", severity="medium",
                                              title=f"{route} does not refresh every {ms / 1000:g}s as the code specifies", expected=t.expected, observed=t.observed,
                                              standard_ref="ISTQB state transition; code-derived oracle", confidence=0.8, reproduced_n=1, reproduced_m=1, status=gate_status(0.8), test_key=t.test_key))
                continue

            # stop control: an "Auto-refresh" checkbox or switch on the page
            toggle = None
            for c in components:
                if c.route == route and c.kind in ("checkbox", "switch") and TOGGLE_RE.search(c.label or c.name or ""):
                    toggle = c
                    break
            loc = None
            if toggle:
                loc = runner.locate(toggle.selector, toggle.role, toggle.name or toggle.label)
            if loc is None:
                lab = page.get_by_label(TOGGLE_RE).first
                with contextlib.suppress(Exception):
                    if lab.count():
                        loc = lab
            if loc is not None:
                tname = (toggle.label if toggle else "auto-refresh") or "auto-refresh"
                with contextlib.suppress(Exception):
                    loc.click()
                page.wait_for_timeout(500)
                mon.reset()
                clock.run_for(ms * 2 + 1000)
                page.wait_for_timeout(800)
                after_off = [x for x in _api_paths(mon.api_requests) if x in polled]
                t2 = TestRecord(test_key=f"timer-stop|{route}", name=f"Turning off '{tname}' on {route} stops polling", technique="lifecycle", category="lifecycle", route=route,
                                expected=f"No polling requests for two periods after '{tname}' is turned off", observed=f"{len(after_off)} polling request(s) in two periods: {', '.join(sorted(set(after_off))) or 'none'}",
                                status="pass" if not after_off else "fail", reproduced_n=1, reproduced_m=1, component_key=toggle.key if toggle else None)
                tests.append(t2)
                if toggle:
                    toggle.status = "pass" if not after_off else "fail"
                    toggle.notes = (toggle.notes + "; " if toggle.notes else "") + "tested with a controlled clock"
                if after_off:
                    findings.append(FindingRecord(fingerprint=fingerprint("timer-stop", route), route=route, category="lifecycle", severity="medium",
                                                  title=f"Turning off '{tname}' on {route} does not stop polling", expected=t2.expected, observed=t2.observed,
                                                  standard_ref="ISTQB state transition; code-derived oracle", confidence=0.85, reproduced_n=1, reproduced_m=1, status=gate_status(0.85), test_key=t2.test_key))
                with contextlib.suppress(Exception):
                    loc.click()
                page.wait_for_timeout(500)

            # cleanup: navigate away in-app, polling for this page must stop
            link = next((c for c in nav_links if c.href and urlparse(c.href).path not in (route,) and "/login" not in c.href), None)
            if link:
                dest = urlparse(link.href).path
                lloc = runner.locate(link.selector, link.role, link.name or link.label)
                if lloc is not None:
                    lloc.click()
                    page.wait_for_timeout(runner.settings.settle_ms)
                    mon.reset()
                    clock.run_for(ms * 2 + 1000)
                    page.wait_for_timeout(800)
                    leaked = [x for x in _api_paths(mon.api_requests) if x in polled]
                    # control: the destination page on its own, fresh
                    baseline = []
                    if leaked:
                        runner.prepare(link.href if link.href.startswith("http") else runner.live_url + dest)
                        mon.reset()
                        clock.run_for(ms * 2 + 1000)
                        page.wait_for_timeout(800)
                        baseline = [x for x in _api_paths(mon.api_requests) if x in polled]
                    leak = len(leaked) > len(baseline)
                    t3 = TestRecord(test_key=f"timer-cleanup|{route}", name=f"Leaving {route} stops its polling", technique="lifecycle", category="lifecycle", route=route,
                                    expected=f"After navigating to {dest}, {route}'s polling stops (interval cleared on unmount)",
                                    observed=f"{len(leaked)} request(s) to {', '.join(polled)} in two periods on {dest}; {dest} alone makes {len(baseline)}",
                                    status="fail" if leak else "pass", reproduced_n=1, reproduced_m=1)
                    tests.append(t3)
                    if leak:
                        findings.append(FindingRecord(fingerprint=fingerprint("timer-leak", route), route=route, category="lifecycle", severity="medium",
                                                      title=f"{route} keeps polling after the user leaves the page", expected=t3.expected, observed=t3.observed,
                                                      standard_ref="ISO/IEC 25010 Resource utilization; code-derived oracle", confidence=0.85, reproduced_n=1, reproduced_m=1,
                                                      status=gate_status(0.85), test_key=t3.test_key))

    # session timeouts declared in code
    for t in inv.timers:
        if not t.ms or not any(LOGOUT_RE.search(c) for c in t.calls):
            continue
        p = next((pg for pg in pages if pg.protected), None)
        if not p:
            continue
        with runner.session(clock=True):
            runner.prepare(p.url)
            runner.ctx.clock.pause_at(runner.page.evaluate("Date.now()") + 500)
            runner.ctx.clock.run_for(int(t.ms) + 2000)
            runner.page.wait_for_timeout(1000)
            on_login = "/login" in urlparse(runner.page.url).path
            tr = TestRecord(test_key=f"session-timeout|{t.file}:{t.line}", name=f"Session ends after {int(t.ms) / 1000:g}s as coded", technique="lifecycle", category="security", route=p.route,
                            expected=f"{t.file}:{t.line} signs the user out after {int(t.ms) / 1000:g}s", observed=f"after the period: {urlparse(runner.page.url).path}",
                            status="pass" if on_login else "fail", reproduced_n=1, reproduced_m=1)
            tests.append(tr)
            if not on_login:
                findings.append(FindingRecord(fingerprint=fingerprint("session-timeout", t.file, str(t.line)), route=p.route, category="security", severity="high",
                                              title=f"Session does not end after the coded {int(t.ms) / 1000:g}s timeout", expected=tr.expected, observed=tr.observed,
                                              standard_ref="OWASP ASVS 3.3.2; WSTG-SESS-07", confidence=0.8, reproduced_n=1, reproduced_m=1, status=gate_status(0.8), test_key=tr.test_key))
    return tests, findings
