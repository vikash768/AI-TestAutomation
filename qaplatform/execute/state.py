"""What must survive and what must not: reload, new tab, Back/Forward, logout.

  reload          a signed-in user stays signed in on the same route
  new tab         shares the session when the code keeps it in localStorage
  back/forward    returns to the previous route with its own content
  logout failure  when the logout request fails, local credentials must still go
  back after out  history must not show protected pages after logout
  unsaved edits   leaving a form with typed changes is warned about (heuristic)
"""
from __future__ import annotations

import contextlib
import re
from urllib.parse import urlparse

from qaplatform.execute.guard import describe
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint, step_of
from qaplatform.explore.browser import goto
from qaplatform.explore.crawler import ObservedPage
from qaplatform.explore.scripts import FORMS_JS, LAYERS_JS, PAGE_STATE_JS, STORAGE_KEYS_JS
from qaplatform.judge.gate import confidence, gate_status

HEADING_JS = "() => { const h = document.querySelector('main h1, h1, main h2, [role=heading]'); return h ? (h.innerText || '').trim().slice(0, 80) : document.title; }"


def _path(u: str) -> str:
    p = urlparse(u).path or "/"
    return p[:-1] if len(p) > 1 and p.endswith("/") else p


def _f(kind, route, key, sev, cat, title, t: TestRecord, ref, conf=0.9, status=None, desc="") -> FindingRecord:
    return FindingRecord(fingerprint=fingerprint(kind, route, key), route=route, category=cat, severity=sev, title=title, description=desc, expected=t.expected,
                         observed=t.observed, standard_ref=ref, confidence=conf, reproduced_n=t.reproduced_n or 1, reproduced_m=t.reproduced_m or 1,
                         status=status or gate_status(conf), test_key=t.test_key)


def run_state(runner: SmokeRunner, pages: list[ObservedPage], components: list[ComponentRecord], protected_routes: list[str], storage_keys: list[str], events) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    protected_pages = [p for p in pages if p.protected and "/login" not in p.route]
    nav_links = [c for c in components if c.route == "(layout)" and c.kind == "link" and c.href and c.page_url and c.visible]
    token_keys = [k for k in storage_keys if re.search(r"token|session|auth|jwt", k, re.I)]

    with runner.session():
        page = runner.page
        # reload persistence on every protected route
        for i, p in enumerate(protected_pages, 1):
            if i % 5 == 0:
                events("state", f"Reload check {i}/{len(protected_pages)}", 87)
            ok, why = runner.prepare(p.url)
            t = TestRecord(test_key=f"reload|{p.route}", name=f"Reloading {p.route} keeps the user there, signed in", technique="persistence", category="persistence", route=p.route,
                           steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "RELOAD"}], expected=f"After reload the URL is still {p.route} and the page renders")
            if not ok:
                t.status, t.observed = "blocked", why
                tests.append(t)
                continue
            heading = page.evaluate(HEADING_JS)
            page.reload(wait_until="domcontentloaded")
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=8000)
            page.wait_for_timeout(runner.settings.settle_ms)
            st = page.evaluate(PAGE_STATE_JS)
            now = st.get("path")
            heading2 = page.evaluate(HEADING_JS)
            t.observed = f"after reload: {now}, heading {heading2!r}"
            t.reproduced_n = t.reproduced_m = 1
            if "/login" in (now or "") and "/login" not in p.route:
                t.status = "fail"
                findings.append(_f("reload-logout", p.route, "", "high", "persistence", f"Reloading {p.route} signs the user out", t, "ISO/IEC 25010 Reliability; OWASP WSTG-SESS"))
            elif _path(now or "") != _path(p.url) or st.get("rootEmpty") or int(st.get("bodyTextLength", 0)) < 30:
                t.status = "fail"
                findings.append(_f("reload-lost", p.route, "", "high", "persistence", f"Reloading {p.route} does not return to the same page", t, "ISTQB state transition"))
            else:
                t.status = "pass"
            tests.append(t)
            runner._dirty = True

        # new tab shares the session (only when the code keeps it in localStorage)
        if token_keys and protected_pages:
            p = protected_pages[0]
            tab = runner.ctx.new_page()
            goto(tab, p.url, runner.settings)
            now = _path(tab.url)
            t = TestRecord(test_key=f"newtab|{p.route}", name=f"A new tab opens {p.route} signed in", technique="persistence", category="persistence", route=p.route,
                           expected="localStorage session (code) is shared with a new tab of the same browser", observed=f"new tab landed on {now}",
                           status="pass" if "/login" not in now else "fail", reproduced_n=1, reproduced_m=1)
            tests.append(t)
            if t.status == "fail":
                findings.append(_f("newtab", p.route, "", "medium", "persistence", "A new browser tab is not signed in", t, "ISO/IEC 25010 Usability",
                                   desc=f"The code keeps the session in localStorage ({', '.join(token_keys)}), which every tab shares."))
            with contextlib.suppress(Exception):
                tab.close()

        # Back / Forward between routes via the app's own navigation
        pairs = []
        for a in protected_pages[:6]:
            link = next((c for c in nav_links if c.href and _path(c.href) not in (a.route,) and "/login" not in _path(c.href)), None)
            if link:
                pairs.append((a, link))
        for a, link in pairs[:4]:
            ok, why = runner.prepare(a.url)
            t = TestRecord(test_key=f"back|{a.route}|{link.label}", name=f"Back from {_path(link.href)} returns to {a.route}", technique="navigation", category="navigation", route=a.route,
                           steps=[{"type": "NAVIGATE", "value": a.url}, {"type": "CLICK", "target": step_of(link)}, {"type": "GO_BACK"}, {"type": "GO_FORWARD"}],
                           expected=f"Back returns to {a.route} with its content; Forward returns to {_path(link.href)}")
            if not ok:
                t.status, t.observed = "blocked", why
                tests.append(t)
                continue
            heading_a = page.evaluate(HEADING_JS)
            loc = runner.locate(link.selector, link.role, link.name or link.label)
            if loc is None:
                t.status, t.observed = "blocked", "nav link not found"
                tests.append(t)
                continue
            loc.click()
            page.wait_for_timeout(runner.settings.settle_ms)
            b_path = _path(page.url)
            page.go_back()
            page.wait_for_timeout(runner.settings.settle_ms)
            back_path, back_heading = _path(page.url), page.evaluate(HEADING_JS)
            page.go_forward()
            page.wait_for_timeout(runner.settings.settle_ms)
            fwd_path = _path(page.url)
            t.observed = f"went to {b_path}; back -> {back_path} ({back_heading!r}); forward -> {fwd_path}"
            ok_back = back_path == _path(a.url) and (not heading_a or back_heading == heading_a)
            ok_fwd = fwd_path == b_path
            t.status = "pass" if ok_back and ok_fwd else "fail"
            t.reproduced_n = t.reproduced_m = 1
            tests.append(t)
            if not ok_back:
                findings.append(_f("back", a.route, link.label, "medium", "navigation", f"Browser Back from {b_path} does not return to {a.route}", t, "ISTQB state transition"))
            elif not ok_fwd:
                findings.append(_f("forward", a.route, link.label, "low", "navigation", f"Browser Forward does not return to {b_path}", t, "ISTQB state transition"))
            runner._dirty = True

        # unsaved edits (heuristic, needs review): type into a form, leave via the nav
        for p in protected_pages:
            ok, _ = runner.prepare(p.url)
            if not ok:
                continue
            forms = [f for f in page.evaluate(FORMS_JS) if any(x["type"] in ("", "text", "textarea", "email") for x in f["fields"]) and not f["inLayer"]]
            if not forms:
                continue
            field = next(x for x in forms[0]["fields"] if x["type"] in ("", "text", "textarea", "email"))
            link = next((c for c in nav_links if c.href and _path(c.href) != p.route), None)
            if not link:
                continue
            probe = "QA unsaved edit"
            with contextlib.suppress(Exception):
                page.locator(field["selector"]).first.fill(probe)
            mon_dialogs = len(runner.mon.dialogs)
            layers0 = {l["signature"] for l in page.evaluate(LAYERS_JS)}
            loc = runner.locate(link.selector, link.role, link.name or link.label)
            if loc is None:
                continue
            loc.click()
            page.wait_for_timeout(runner.settings.settle_ms)
            warned = len(runner.mon.dialogs) > mon_dialogs or any(l["signature"] not in layers0 for l in page.evaluate(LAYERS_JS)) or _path(page.url) == p.route
            t = TestRecord(test_key=f"unsaved|{p.route}", name=f"Leaving {p.route} with unsaved edits warns first", technique="persistence", category="ux", route=p.route,
                           expected="Typed but unsaved changes are not discarded silently", observed=f"typed into '{field['label']}', clicked '{link.label}': " + ("warned or stayed" if warned else f"left to {_path(page.url)} without warning"),
                           status="pass" if warned else "fail", reproduced_n=1, reproduced_m=1)
            tests.append(t)
            if not warned:
                findings.append(_f("unsaved", p.route, field["label"], "low", "ux", f"Unsaved changes on {p.route} are discarded without warning", t,
                                   "Nielsen #5 Error prevention", conf=0.6, status="needs_review"))
            runner._dirty = True

        # logout when the logout request fails (guard blocks it): local credentials must still be cleared
        logout = next((c for c in components if c.action_class == "session_ending" and c.page_url and c.visible), None)
        if logout:
            ok, why = runner.prepare(logout.page_url)
            loc = runner.locate(logout.selector, logout.role, logout.name or logout.label) if ok else None
            t = TestRecord(test_key="logout-offline", name="Logging out while the server is unreachable still signs out locally", technique="session", category="security", route=logout.route,
                           steps=[{"type": "NAVIGATE", "value": logout.page_url}, {"type": "CLICK", "target": step_of(logout)}],
                           expected="The local session is cleared and the user is sent to sign-in, even if the logout request fails")
            if loc is None:
                t.status, t.observed = "blocked", why or "logout control not found"
                tests.append(t)
            else:
                results = []
                for attempt in range(2):
                    if attempt:
                        runner.prepare(logout.page_url)
                        loc = runner.locate(logout.selector, logout.role, logout.name or logout.label)
                        if loc is None:
                            break
                    mark = runner.guard.mark()
                    keys_before = page.evaluate(STORAGE_KEYS_JS)
                    loc.click()
                    page.wait_for_timeout(runner.settings.settle_ms * 2)
                    blocked = runner.guard.since(mark)
                    keys_after = page.evaluate(STORAGE_KEYS_JS)
                    left = [k for k in token_keys if k in keys_after]
                    on_login = "/login" in _path(page.url)
                    results.append((on_login and not left, blocked, left, _path(page.url)))
                    if on_login and not left:
                        break
                    runner._relogin(runner.ctx)
                ok_all = results and results[-1][0]
                last = results[-1] if results else (False, [], [], "")
                t.observed = f"logout request {'blocked: ' + describe(last[1]) if last[1] else 'not sent'}; now on {last[3]}; credentials left in storage: {', '.join(last[2]) or 'none'}"
                t.status = "pass" if ok_all else "fail"
                t.reproduced_n = sum(1 for r in results if not r[0]) if not ok_all else 1
                t.reproduced_m = len(results)
                tests.append(t)
                if not ok_all and last[1]:
                    f = _f("logout-offline", logout.route, "", "medium", "security", "Logout keeps the session when the logout request fails", t,
                           "OWASP WSTG-SESS-06 Testing for Logout Functionality; OWASP ASVS 3.3.1",
                           desc="The logout request was made to fail in the browser (as on a dropped connection). The app should still clear local credentials and leave the signed-in area.")
                    f.confidence = confidence(t.reproduced_n, t.reproduced_m)
                    f.status = gate_status(f.confidence)
                    findings.append(f)
            runner._relogin(runner.ctx)
            runner._dirty = True

        # back after a real logout must not show protected content
        if logout and protected_pages:
            p = protected_pages[0]
            with runner.guard.suspended():
                ok, _ = runner.prepare(p.url)
                loc = runner.locate(logout.selector, logout.role, logout.name or logout.label) if ok else None
                if loc is not None:
                    loc.click()
                    page.wait_for_timeout(runner.settings.settle_ms * 2)
                    page.go_back()
                    page.wait_for_timeout(runner.settings.settle_ms * 2)
                    st = page.evaluate(PAGE_STATE_JS)
                    shows_login = bool(st.get("hasPasswordField")) or "/login" in (st.get("path") or "")
                    t = TestRecord(test_key="back-after-logout", name="Back after logout does not show protected pages", technique="session", category="security", route=p.route,
                                   steps=[{"type": "NAVIGATE", "value": p.url}, {"type": "CLICK", "target": step_of(logout)}, {"type": "GO_BACK"}],
                                   expected="After logout, Back shows the sign-in page, not the previous protected page",
                                   observed=f"after Back: {st.get('path')}, sign-in form {'shown' if shows_login else 'not shown'}",
                                   status="pass" if shows_login else "fail", reproduced_n=1, reproduced_m=1)
                    tests.append(t)
                    if not shows_login:
                        findings.append(_f("back-logout", p.route, "", "high", "security", "Protected page is shown from browser history after logout", t, "OWASP WSTG-ATHN-06 Browser Cache Weaknesses"))
            runner._relogin(runner.ctx)
    return tests, findings
