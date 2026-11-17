"""What the user sees when the backend fails.

Only each page's own data requests are failed (requests every page makes, such
as session bootstrap, are left alone), and only reads: writes are already
blocked by the guard.

  502       page must not go blank; it must say something went wrong
  retry     if a Retry is offered (the code usually declares one), it must recover
  offline   same oracle with the connection dropped, on pages that offer Retry
  slow      while data is pending, the page must not claim there is no data
"""
from __future__ import annotations

import contextlib
import re
from collections import Counter

from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint
from qaplatform.explore.crawler import ObservedPage
from qaplatform.explore.scripts import FEEDBACK_JS, PAGE_STATE_JS
from qaplatform.ingest.js_parser import CodeElement
from qaplatform.judge.gate import confidence, gate_status

RETRY_RE = re.compile(r"^(retry|try again|reload|refresh)$", re.I)


def page_specific_reads(pages: list[ObservedPage]) -> dict[str, set[str]]:
    protected = [p for p in pages if p.protected]
    counts = Counter()
    per = {}
    for p in protected:
        paths = {r["path"] for r in p.api_requests if r.get("method") == "GET"}
        per[p.route] = paths
        counts.update(paths)
    common = {path for path, n in counts.items() if n >= max(2, int(0.8 * len(protected)))}
    return {route: paths - common for route, paths in per.items()}


def run_faults(runner: SmokeRunner, pages: list[ObservedPage], components: list[ComponentRecord], code_elements: list[CodeElement], events, max_pages: int = 20) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    targets = page_specific_reads(pages)
    pages = [p for p in pages if p.protected and targets.get(p.route)][:max_pages]
    code_retry_routes = {e.route for e in code_elements if RETRY_RE.match((e.label or "").strip())}
    with runner.session():
        page, guard = runner.page, runner.guard
        for i, p in enumerate(pages, 1):
            events("faults", f"Failing {p.route}'s data requests ({i}/{len(pages)})", 89)
            own = targets[p.route]
            # baseline
            ok, why = runner.prepare(p.url)
            if not ok:
                tests.append(TestRecord(test_key=f"fault-502|{p.route}", name=f"{p.route} handles a failing backend", technique="fault-injection", category="stability", route=p.route,
                                        expected="Error state", observed=why, status="blocked"))
                continue
            base = page.evaluate(FEEDBACK_JS)

            def only_own(route_, req, mode):
                from urllib.parse import urlparse
                if req.resource_type in ("fetch", "xhr") and req.method == "GET" and urlparse(req.url).path in own:
                    guard.injected.append({"mode": mode, "url": req.url[:200]})
                    if mode == "502":
                        route_.fulfill(status=502, content_type="text/html", body="<html><body><h1>502 Bad Gateway</h1></body></html>")
                    elif mode == "offline":
                        route_.abort("internetdisconnected")
                    else:
                        guard.held.append(route_)
                    return True
                return False

            for mode in ("502", "offline"):
                if mode == "offline" and p.route not in code_retry_routes:
                    continue
                guard.injection = lambda r, q, m=mode: only_own(r, q, m)
                guard.injected.clear()
                runner.mon.reset()
                ok, why = runner.prepare(p.url)
                page.wait_for_timeout(2000)
                fb = page.evaluate(FEEDBACK_JS)
                st = page.evaluate(PAGE_STATE_JS)
                errs = runner.mon.snapshot()["page_errors"]
                injected = len(guard.injected)
                new_err = [t for t in fb["errors"] + fb["alerts"] if t not in base["errors"] and t not in base["alerts"]]
                blank = bool(st.get("rootEmpty")) or fb["textLength"] < 30
                label = "returns 502" if mode == "502" else "is unreachable"
                t = TestRecord(test_key=f"fault-{mode}|{p.route}", name=f"{p.route} when its API {label}", technique="fault-injection", category="stability", route=p.route,
                               steps=[{"type": "INJECT", "value": f"{mode} on GET {', '.join(sorted(own))[:200]}"}, {"type": "NAVIGATE", "value": p.url}],
                               expected="The page stays usable and tells the user the data could not be loaded")
                t.reproduced_n = t.reproduced_m = 1
                if not ok or injected == 0:
                    t.status, t.observed = "blocked", why or "no request matched the injection"
                    tests.append(t)
                    guard.clear_injection()
                    continue
                if blank:
                    t.status, t.observed = "fail", f"page went blank ({fb['textLength']} chars){'; uncaught: ' + errs[0][:120] if errs else ''}"
                    findings.append(FindingRecord(fingerprint=fingerprint("fault-blank", p.route), route=p.route, category="stability", severity="high",
                                                  title=f"{p.route} goes blank when its data request fails", expected=t.expected, observed=t.observed,
                                                  standard_ref="ISO/IEC 25010 Fault tolerance", confidence=0.9, reproduced_n=1, reproduced_m=1,
                                                  evidence=[x for x in [runner.screenshot(f"fault_{p.route}")] if x], test_key=t.test_key))
                elif new_err or fb["retry"]:
                    t.status, t.observed = "pass", f"showed {(new_err or fb['retry'])[0][:100]!r}"
                elif fb["loading"]:
                    t.status, t.observed = "fail", "still shows a loading state 2 s after the request failed"
                    findings.append(FindingRecord(fingerprint=fingerprint("fault-stuck", p.route), route=p.route, category="ux", severity="medium",
                                                  title=f"{p.route} stays on its loading state when the data request fails", expected=t.expected, observed=t.observed,
                                                  standard_ref="Nielsen #1 Visibility of system status", confidence=0.85, reproduced_n=1, reproduced_m=1, test_key=t.test_key))
                else:
                    known = p.route in code_retry_routes
                    t.status, t.observed = "fail", "no error message, alert or retry appeared"
                    conf = 0.85 if known else 0.6
                    findings.append(FindingRecord(fingerprint=fingerprint("fault-silent", p.route), route=p.route, category="ux", severity="medium",
                                                  title=f"{p.route} does not tell the user its data failed to load", expected=t.expected, observed=t.observed,
                                                  description="The code declares an error state with Retry for this page." if known else "",
                                                  standard_ref="Nielsen #9 Help users recognize, diagnose, and recover from errors", confidence=conf, reproduced_n=1, reproduced_m=1,
                                                  status=gate_status(conf), test_key=t.test_key))
                tests.append(t)

                # recovery through Retry once the backend is back
                if fb["retry"] and not blank:
                    guard.clear_injection()
                    rname = fb["retry"][0]
                    with contextlib.suppress(Exception):
                        page.get_by_role("button", name=re.compile("^" + re.escape(rname) + "$", re.I)).first.click(timeout=3000)
                    page.wait_for_timeout(runner.settings.settle_ms + 800)
                    fb2 = page.evaluate(FEEDBACK_JS)
                    still = [x for x in fb2["errors"] + fb2["alerts"] if x in new_err]
                    recovered = not still and fb2["textLength"] >= 0.7 * base["textLength"]
                    t2 = TestRecord(test_key=f"fault-retry-{mode}|{p.route}", name=f"'{rname}' on {p.route} recovers once the API is back", technique="fault-injection", category="functional",
                                    route=p.route, expected="After Retry the error clears and the data shows", observed=f"error {'still shown' if still else 'cleared'}; {fb2['textLength']} chars vs {base['textLength']} normally",
                                    status="pass" if recovered else "fail", reproduced_n=1, reproduced_m=1)
                    tests.append(t2)
                    for c in components:
                        if c.route == p.route and RETRY_RE.match((c.label or "").strip()) and c.status in ("not_observed", "observed", "untested"):
                            c.status = "pass" if recovered else "fail"
                            c.source = "both" if c.source == "code" else c.source
                            c.page_url = c.page_url or p.url
                            c.notes = f"shown and clicked during fault injection ({mode}); " + ("recovered" if recovered else "did not recover")
                    if not recovered:
                        findings.append(FindingRecord(fingerprint=fingerprint("fault-retry", p.route), route=p.route, category="functional", severity="medium",
                                                      title=f"'{rname}' on {p.route} does not recover after the API is back", expected=t2.expected, observed=t2.observed,
                                                      standard_ref="ISO/IEC 25010 Recoverability", confidence=0.85, reproduced_n=1, reproduced_m=1, test_key=t2.test_key))
                guard.clear_injection()

            # slow backend: no false "no data" while loading (only meaningful when the page has data)
            if not base["empties"]:
                guard.injection = lambda r, q: only_own(r, q, "hold")
                guard.injected.clear()
                with contextlib.suppress(Exception):
                    page.goto(p.url, wait_until="domcontentloaded")
                page.wait_for_timeout(1800)
                fb = page.evaluate(FEEDBACK_JS)
                held = len(guard.held)
                guard.clear_injection()
                page.wait_for_timeout(runner.settings.settle_ms)
                runner._dirty = True
                if held:
                    false_empty = [x for x in fb["empties"] if x not in base["empties"]]
                    t3 = TestRecord(test_key=f"fault-slow|{p.route}", name=f"{p.route} while its data is still loading", technique="fault-injection", category="ux", route=p.route,
                                    expected="A loading state, never a claim that there is no data", observed=(f"said {false_empty[0]!r} while loading" if false_empty else ("loading indicator shown" if fb["loading"] else "no loading indicator, no false empty state")),
                                    status="fail" if false_empty else "pass", reproduced_n=1, reproduced_m=1)
                    tests.append(t3)
                    if false_empty:
                        findings.append(FindingRecord(fingerprint=fingerprint("fault-false-empty", p.route), route=p.route, category="ux", severity="medium",
                                                      title=f"{p.route} says there is no data while it is still loading", expected=t3.expected, observed=t3.observed,
                                                      standard_ref="Nielsen #1 Visibility of system status", confidence=0.85, reproduced_n=1, reproduced_m=1, test_key=t3.test_key))
            runner._dirty = True
    return tests, findings
