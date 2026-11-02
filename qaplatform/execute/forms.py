"""Forms: sign-in negative tests, validation by equivalence partitions and
boundaries, write-path wiring, and failure feedback.

Every form submit here except the sign-in tests runs under the write guard, so
the request a form sends is recorded and blocked: it never reaches the server.
That gives three oracles per form:

  invalid input  -> nothing may be sent (the browser or the app must stop it)
  valid input    -> a write must be sent (the form is wired to the backend)
  write fails    -> the user must be told (it was blocked, so it did fail)
"""
from __future__ import annotations

import contextlib
from pathlib import Path
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from qaplatform.config import Settings
from qaplatform.execute.guard import describe, matches_any
from qaplatform.execute.reveal_rules import NEVER_OPEN_RE
from qaplatform.execute.smoke import CLOSE_RE, ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint
from qaplatform.explore.browser import BrowserSession, attach_monitor, goto
from qaplatform.explore.crawler import ObservedPage
from qaplatform.explore.scripts import FEEDBACK_JS, FIELD_VALIDITY_JS, FORMS_JS, PAGE_STATE_JS
from qaplatform.judge.gate import confidence, gate_status

LEAK_RE = re.compile(r"(stack|trace|exception|\bat [\w.$<>]+ \(|\.js:\d+|\.py\b|SQL|SELECT |syntax error|ECONN|ETIMEDOUT|undefined|\bnull\b|NaN|internal server|errno|jwt|bcrypt|sequelize|prisma|mongo)", re.I)
SAVING_RE = re.compile(r"(saving|submitting|working|please wait|loading)", re.I)
TEXTUAL = {"", "text", "search", "textarea", "tel", "url"}


@dataclass
class FormState:
    route: str
    page_url: str
    reveal: list = field(default_factory=list)
    protected: bool = True


def _form_sig(form: dict) -> tuple:
    return (tuple(f["label"].lower() for f in form["fields"]), (form["submit"] or {}).get("name", "").lower())


def _form_name(form: dict) -> str:
    return (form["submit"] or {}).get("name") or "form"


def sample_value(f: dict, tag: str) -> str | None:
    t, label = f["type"], f["label"].lower()
    if f["tag"] == "select":
        opts = [o for o in (f["options"] or []) if o["value"] not in ("", None)]
        if f["value"] and any(o["value"] == f["value"] for o in opts):
            return f["value"]
        return opts[0]["value"] if opts else None
    if t in ("checkbox", "radio", "file", "range", "color"):
        return None
    if t == "email" or "email" in label:
        return f"qa.probe+{tag}@example.com"
    if t == "password":
        n = max(int(f.get("minLength") or 0), 12)
        return ("QaProbe#2026x" + "y" * n)[:max(n, 13)]
    if t == "number":
        lo = float(f["min"]) if f.get("min") not in (None, "") else None
        hi = float(f["max"]) if f.get("max") not in (None, "") else None
        v = lo if lo is not None else (min(5.0, hi) if hi is not None else 5.0)
        return str(int(v)) if float(v).is_integer() else str(v)
    if t == "date":
        return time.strftime("%Y-%m-%d")
    if t == "time":
        return "09:00"
    if t == "url":
        return "https://example.com/qa-probe"
    if t == "tel":
        return "+15550100"
    v = f"QA probe {tag}"
    if f.get("minLength"):
        v = v.ljust(int(f["minLength"]), "x")
    if f.get("maxLength"):
        v = v[: int(f["maxLength"])]
    return v


def invalid_cases(f: dict) -> list[tuple[str, str, str]]:
    """(case, value, severity) from the field's constraints: equivalence partitions and boundaries."""
    cases: list[tuple[str, str, str]] = []
    t = f["type"]
    if f["tag"] == "select":
        if f["required"] and any(o["value"] == "" for o in (f["options"] or [])):
            cases.append(("left unselected", "", "medium"))
        return cases
    if t in ("checkbox", "radio", "file", "range", "color", "hidden"):
        return cases
    if f["required"]:
        cases.append(("left empty", "", "medium"))
        if t in TEXTUAL or f["tag"] == "textarea":
            cases.append(("filled with spaces only", "   ", "low"))
    if t == "email":
        cases.append(("without an @", "qa.probe.example.com", "medium"))
        cases.append(("without a domain", "qa.probe@", "medium"))
    if t == "number":
        if f.get("min") not in (None, ""):
            with contextlib.suppress(ValueError):
                cases.append((f"below its minimum of {f['min']}", str(int(float(f["min"])) - 1), "medium"))
        if f.get("max") not in (None, ""):
            with contextlib.suppress(ValueError):
                cases.append((f"above its maximum of {f['max']}", str(int(float(f["max"])) + 1), "medium"))
    if f.get("minLength") and int(f["minLength"]) > 1:
        cases.append((f"shorter than its minimum length of {f['minLength']}", "x" * (int(f["minLength"]) - 1), "medium"))
    return cases


# Visible text of the content area; typing into inputs and focus/blur restyles do not change it.
CONTENT_TEXT_JS = "() => { const m = document.querySelector('main') || document.body; return (m.innerText || '').replace(/\\s+/g, ' ').slice(0, 20000); }"
QUERY_SUBMIT_RE = re.compile(r"\b(search|filter|apply|find|look ?up|show results|go)\b", re.I)
QUERY_FIELD_RE = re.compile(r"search|filter|query|keyword|from date|to date|date range", re.I)


def is_query_form(form: dict) -> bool:
    """A search/filter form reads (GET) instead of writing, so 'nothing was saved' is expected for it."""
    if QUERY_SUBMIT_RE.search((form.get("submit") or {}).get("name") or ""):
        return True
    return any(f.get("type") == "search" or QUERY_FIELD_RE.search(f"{f.get('label') or ''} {f.get('name') or ''}") for f in form.get("fields") or [])


def _req_key(url: str) -> str:
    u = urlparse(url)
    return u.path + ("?" + u.query if u.query else "")


class FormTester:
    def __init__(self, runner: SmokeRunner, events, write_paths_by_route: dict[str, list[str]], tag: str):
        self.r = runner
        self.events = events
        self.write_paths = write_paths_by_route
        self.tag = tag

    # -- plumbing ---------------------------------------------------------------
    def _load(self, state: FormState, sig: tuple) -> dict | None:
        ok, _ = self.r.prepare(state.page_url, state.reveal)
        self.r._dirty = True
        if not ok:
            return None
        for form in self.r.page.evaluate(FORMS_JS):
            if _form_sig(form) == sig:
                return form
        return None

    def _set(self, f: dict, value: str) -> bool:
        loc = self.r.page.locator(f["selector"]).first
        try:
            if f["tag"] == "select":
                loc.select_option(value=value, timeout=self.r.settings.action_timeout_ms)
            else:
                loc.fill(value, timeout=self.r.settings.action_timeout_ms)
            return True
        except Exception:
            return False

    def _fill_valid(self, form: dict) -> tuple[bool, str]:
        for f in form["fields"]:
            if f.get("type") == "file":
                # an inert sample file; the guard blocks the upload, so nothing leaves the browser
                path = Path(self.r.run_dir) / f"qa-sample-{self.tag}.bin"
                with contextlib.suppress(Exception):
                    path.write_bytes(b"QA sample file - never uploaded (write guard on)\n")
                    self.r.page.locator(f["selector"]).first.set_input_files(str(path), timeout=self.r.settings.action_timeout_ms)
                    continue
                return False, f"could not attach a file to '{f['label']}'"
            v = sample_value(f, self.tag)
            if v is None:
                if f["tag"] == "select" and f["required"]:
                    return False, f"no option to choose in '{f['label']}'"
                continue
            if not self._set(f, v):
                return False, f"could not fill '{f['label']}'"
        return True, ""

    def _submit(self, form: dict) -> dict:
        """Click submit; record writes (guarded), new GET queries, and whether the visible text changed."""
        page = self.r.page
        fb_before = page.evaluate(FEEDBACK_JS)
        text_before = page.evaluate(CONTENT_TEXT_JS)
        background: list[str] = []
        on_bg = lambda req: background.append(_req_key(req.url)) if req.method == "GET" and req.resource_type in ("xhr", "fetch") else None
        page.on("request", on_bg)
        page.wait_for_timeout(800)  # what the page fetches on its own (polling) is not a query result
        with contextlib.suppress(Exception):
            page.remove_listener("request", on_bg)
        mark = self.r.guard.mark()
        reads: list[str] = []
        on_req = lambda req: reads.append(_req_key(req.url)) if req.method == "GET" and req.resource_type in ("xhr", "fetch") and _req_key(req.url) not in background else None
        btn = page.locator(form["submit"]["selector"]).first
        try:
            if btn.is_disabled():
                return {"clicked": False, "disabled": True, "writes": [], "reads": [], "fb_before": fb_before, "fb_after": fb_before}
            page.on("request", on_req)
            btn.click(timeout=self.r.settings.action_timeout_ms)
        except Exception as exc:
            return {"clicked": False, "error": f"{type(exc).__name__}", "writes": [], "reads": [], "fb_before": fb_before, "fb_after": fb_before}
        page.wait_for_timeout(self.r.settings.settle_ms)
        with contextlib.suppress(Exception):
            page.remove_listener("request", on_req)
        fb_after = page.evaluate(FEEDBACK_JS)
        writes = self.r.guard.since(mark)
        if writes and not self._new_feedback(fb_before, fb_after):
            page.wait_for_timeout(2500)  # give a failure message time to appear
            fb_after = page.evaluate(FEEDBACK_JS)
        changed = page.evaluate(CONTENT_TEXT_JS) != text_before
        return {"clicked": True, "writes": writes, "reads": reads, "changed": changed, "fb_before": fb_before, "fb_after": fb_after}

    @staticmethod
    def _new_feedback(before: dict, after: dict) -> list[str]:
        old = set(before["alerts"]) | set(before["toasts"]) | set(before["errors"])
        new = [t for t in after["alerts"] + after["toasts"] + after["errors"] if t and t not in old]
        return list(dict.fromkeys(new))

    # -- discovery --------------------------------------------------------------------
    def discover(self, pages: list[ObservedPage], components: list[ComponentRecord], protected_routes: list[str], login_path: str) -> list[tuple[FormState, dict]]:
        states = [FormState(p.route, p.url, list(p.reveal), p.route in protected_routes or bool(p.protected)) for p in pages if p.route not in (login_path,) and p.protected]
        seen_paths = set()
        for c in components:
            s = c.smoke_signals or {}
            # Openers of a form: a dialog/menu ("opened"), or a toggle that reveals an inline
            # composer in place ("Add operator", "Upload firmware"). A button classed as mutating
            # by its name ("Reset password") qualifies when its click sent no write — it only opened UI.
            in_place = s.get("changed") and not s.get("url_changed") and not s.get("popups")
            opener = s.get("opened") or (c.intent or {}).get("opens") or in_place
            harmless = (c.action_class == "safe" or (c.action_class == "mutating" and s and not s.get("writes"))) and not NEVER_OPEN_RE.search(c.label or c.name or "")
            if c.page_url and opener and harmless and c.selector and not CLOSE_RE.match((c.label or c.name or "").strip()):
                path = tuple(x.get("name") for x in c.reveal) + (c.name or c.label,)
                if (c.route, path) in seen_paths:
                    continue
                seen_paths.add((c.route, path))
                from qaplatform.execute.smoke import step_of
                states.append(FormState(c.route, c.page_url, c.reveal + [step_of(c)], True))
        found: dict[tuple, tuple[FormState, dict]] = {}
        for i, st in enumerate(states, 1):
            if i % 10 == 0:
                self.events("forms", f"Looking for forms {i}/{len(states)}", 83)
            ok, _ = self.r.prepare(st.page_url, st.reveal)
            self.r._dirty = True
            if not ok:
                continue
            for form in self.r.page.evaluate(FORMS_JS):
                if not form["submit"] or not form["fields"]:
                    continue
                if any(f["type"] == "password" for f in form["fields"]) and not st.protected:
                    continue  # the sign-in form has its own tests
                if re.search(r"\b(delete|remove|purge|clean|destroy|wipe|reset)\b", form["submit"]["name"] or "", re.I):
                    continue  # never submit destructive forms, even guarded
                key = _form_sig(form)
                if key not in found:
                    found[key] = (st, form)
        return list(found.values())

    # -- tests ---------------------------------------------------------------------------------
    def test_form(self, st: FormState, form: dict) -> tuple[list[TestRecord], list[FindingRecord]]:
        tests: list[TestRecord] = []
        findings: list[FindingRecord] = []
        name = _form_name(form)
        sig = _form_sig(form)
        where = st.route + (f" (after {' > '.join(s.get('name') or '?' for s in st.reveal)})" if st.reveal else "")
        steps_head = [{"type": "NAVIGATE", "value": st.page_url}] + [{"type": "CLICK", "target": s, "description": "reveal"} for s in st.reveal]

        # 1. valid submit: wiring + failure feedback
        loaded = self._load(st, sig)
        if loaded is None:
            return [TestRecord(test_key=f"form|{st.route}|{name}", name=f"'{name}' form on {where}", technique="form-wiring", category="functional", route=st.route,
                               expected="Form reachable", observed="form not found on reload", status="blocked")], []
        ok, why = self._fill_valid(loaded)
        wiring = TestRecord(test_key=f"form-wiring|{st.route}|{name}", name=f"Valid '{name}' submits a write on {where}", technique="form-wiring", category="functional",
                            route=st.route, steps=steps_head + [{"type": "FILL", "description": "valid sample values"}, {"type": "CLICK", "target": form["submit"]}],
                            expected="With valid input the form sends its write (intercepted, never reaches the server)")
        if not ok:
            wiring.status, wiring.observed = "blocked", why
            tests.append(wiring)
        else:
            res = self._submit(loaded)
            writes = res["writes"]
            new_fb = self._new_feedback(res["fb_before"], res["fb_after"])
            if writes:
                hit = matches_any(writes[0].path, self.write_paths.get(st.route, []) + self.write_paths.get("(layout)", []))
                wiring.status = "pass"
                wiring.observed = f"sent {describe(writes)}" + (f" (matches code {hit})" if hit else " (endpoint not found in this page's code)") + f"; fields sent: {', '.join(writes[0].body_keys) or '-'}"
                wiring.reproduced_n = wiring.reproduced_m = 1
                tests.append(wiring)
                # failure feedback: the write was blocked, so the app must say so
                fb = TestRecord(test_key=f"form-failure|{st.route}|{name}", name=f"'{name}' tells the user when saving fails on {where}", technique="failure-feedback",
                                category="ux", route=st.route, steps=wiring.steps, expected="A failed save shows an error message and the form becomes usable again")
                successes = [t for t in res["fb_after"]["successes"] if t not in res["fb_before"]["successes"]]
                stuck = False
                with contextlib.suppress(Exception):
                    b = self.r.page.locator(form["submit"]["selector"]).first
                    if b.count():
                        txt = b.inner_text(timeout=1000)
                        stuck = b.is_disabled() and bool(SAVING_RE.search(txt or ""))
                if successes:
                    fb.status, fb.observed = "fail", f"shows success although the request failed: {successes[0]!r}"
                    findings.append(self._finding("form-false-success", st, name, "high", "functional", f"'{name}' on {st.route} reports success although the save failed",
                                                  fb, "ISO/IEC 25010 Functional correctness; Nielsen #1 Visibility of system status"))
                elif stuck:
                    fb.status, fb.observed = "fail", "submit button stays in its saving state"
                    findings.append(self._finding("form-stuck", st, name, "medium", "functional", f"'{name}' on {st.route} stays in its saving state after a failed request",
                                                  fb, "Nielsen #1 Visibility of system status"))
                elif new_fb:
                    fb.status, fb.observed = "pass", f"showed: {new_fb[0][:120]!r}"
                else:
                    # confirm on a fresh form before calling it
                    again = self._load(st, sig)
                    repeat_silent = False
                    if again is not None and self._fill_valid(again)[0]:
                        r2 = self._submit(again)
                        repeat_silent = bool(r2["writes"]) and not self._new_feedback(r2["fb_before"], r2["fb_after"])
                    fb.status, fb.observed = "fail", "no message, alert or toast appeared after the write failed"
                    fb.reproduced_n, fb.reproduced_m = (2 if repeat_silent else 1), 2
                    f = self._finding("form-silent-failure", st, name, "medium", "ux", f"'{name}' on {st.route} gives no feedback when saving fails", fb,
                                      "Nielsen #9 Help users recognize, diagnose, and recover from errors; ISO/IEC 25010 Fault tolerance")
                    f.confidence = confidence(fb.reproduced_n, fb.reproduced_m)
                    f.status = gate_status(f.confidence)
                    findings.append(f)
                if not fb.reproduced_m:
                    fb.reproduced_n = fb.reproduced_m = 1
                tests.append(fb)
            else:
                invalid_now = []
                for fl in loaded["fields"]:
                    v = self.r.page.evaluate(FIELD_VALIDITY_JS, fl["selector"])
                    if v and not v["valid"]:
                        invalid_now.append(f"{fl['label']}: {v['message']}")
                if res.get("disabled"):
                    wiring.status, wiring.observed = "blocked", "submit button disabled after filling valid values"
                elif invalid_now:
                    wiring.status, wiring.observed = "blocked", f"sample value rejected by the browser: {invalid_now[0][:120]}"
                elif new_fb:
                    wiring.status, wiring.observed = "blocked", f"app rejected the sample values: {new_fb[0][:120]!r}"
                else:
                    st_now = self.r.page.evaluate(PAGE_STATE_JS)
                    if urlparse(st.page_url).path != st_now.get("path"):
                        wiring.status, wiring.observed = "pass", f"navigated to {st_now.get('path')} (no write)"
                    elif is_query_form(form) and (res.get("reads") or res.get("changed")):
                        # A search/filter form queries (GET) or updates the page in place; it has no write to send.
                        wiring.name = f"Valid '{name}' runs its query on {where}"
                        wiring.expected = "With valid input the form queries or updates the page"
                        wiring.status = "pass"
                        wiring.observed = (f"fetched {', '.join(dict.fromkeys(res['reads']))[:160]}" if res.get("reads") else "updated the results in place") + " (no write)"
                    else:
                        wiring.status, wiring.observed = "fail", "nothing was sent and nothing was shown"
                        findings.append(self._finding("form-dead", st, name, "medium", "functional", f"Submitting a valid '{name}' form on {st.route} does nothing", wiring,
                                                      "Nielsen #1 Visibility of system status"))
                wiring.reproduced_n = wiring.reproduced_m = 1
                tests.append(wiring)

        # 2. invalid partitions per field: nothing may be sent
        for fl in form["fields"]:
            for case, value, sev in invalid_cases(fl):
                t = TestRecord(test_key=f"form-validation|{st.route}|{name}|{fl['label']}|{case}", name=f"'{name}' rejects {fl['label']} {case}", technique="validation",
                               category="validation", route=st.route,
                               steps=steps_head + [{"type": "FILL", "description": "valid sample values"}, {"type": "FILL", "target": {"selector": fl["selector"]}, "value": value or "(empty)"}, {"type": "CLICK", "target": form["submit"]}],
                               expected=f"Submit is stopped and nothing is sent ({'required' if 'empty' in case or 'unselected' in case else fl['type'] or 'constraint'} in the form)")
                outcome = self._invalid_attempt(st, sig, fl, value)
                if outcome is None:
                    t.status, t.observed = "blocked", "form could not be prepared"
                    tests.append(t)
                    continue
                sent, observed = outcome
                t.observed = observed
                if sent:
                    again = self._invalid_attempt(st, sig, fl, value)
                    n = 2 if again and again[0] else 1
                    t.status, t.reproduced_n, t.reproduced_m = "fail", n, 2
                    f = self._finding("validation", st, f"{name}|{fl['label']}|{case}", sev, "validation", f"'{name}' on {st.route} accepts {fl['label']} {case}", t,
                                      "ISTQB equivalence partitioning / boundary value analysis; " + ("WCAG 2.2 3.3.1 Error Identification" if sev != "low" else "OWASP ASVS 5.1.3 input validation"))
                    f.confidence = confidence(n, 2)
                    f.status = gate_status(f.confidence)
                    findings.append(f)
                else:
                    t.status, t.reproduced_n, t.reproduced_m = "pass", 1, 1
                tests.append(t)
        return tests, findings

    def _invalid_attempt(self, st: FormState, sig: tuple, fl: dict, value: str) -> tuple[bool, str] | None:
        form = self._load(st, sig)
        if form is None:
            return None
        ok, _ = self._fill_valid(form)
        target = next((f for f in form["fields"] if f["label"] == fl["label"]), None)
        if not ok or target is None or not self._set(target, value):
            return None
        res = self._submit(form)
        validity = self.r.page.evaluate(FIELD_VALIDITY_JS, target["selector"]) or {}
        new_fb = self._new_feedback(res["fb_before"], res["fb_after"])
        if res["writes"]:
            return True, f"sent {describe(res['writes'])} (intercepted)"
        why = validity.get("message") or (new_fb[0] if new_fb else "") or ("submit disabled" if res.get("disabled") else "not submitted")
        return False, f"stopped: {why[:120]}"

    @staticmethod
    def _finding(kind: str, st: FormState, key: str, sev: str, cat: str, title: str, test: TestRecord, ref: str) -> FindingRecord:
        reveal = (" Reached by clicking: " + " > ".join(s.get("name") or "?" for s in st.reveal) + ".") if st.reveal else ""
        return FindingRecord(fingerprint=fingerprint(kind, st.route, key), route=st.route, category=cat, severity=sev, title=title,
                             description=f"Tested with the write guard on: requests are recorded and blocked in the browser.{reveal}",
                             expected=test.expected, observed=test.observed, standard_ref=ref, confidence=0.9, reproduced_n=test.reproduced_n or 1,
                             reproduced_m=test.reproduced_m or 1, test_key=test.test_key)

    def run(self, pages: list[ObservedPage], components: list[ComponentRecord], protected_routes: list[str], login_path: str) -> tuple[list[TestRecord], list[FindingRecord], dict]:
        tests: list[TestRecord] = []
        findings: list[FindingRecord] = []
        with self.r.session():
            forms = self.discover(pages, components, protected_routes, login_path)
            for i, (st, form) in enumerate(forms, 1):
                self.events("forms", f"Testing form {i}/{len(forms)}: '{_form_name(form)}' on {st.route}", 84 + int(3 * i / max(len(forms), 1)))
                t, f = self.test_form(st, form)
                tests.extend(t)
                findings.extend(f)
                self._mark_components(components, st, form, t)
        return tests, findings, {"forms": len(forms)}

    @staticmethod
    def _mark_components(components: list[ComponentRecord], st: FormState, form: dict, tests: list[TestRecord]) -> None:
        """Credit each field (and the submit button) with the outcome of ITS tests. A field is
        identified on the same screen by its label or placeholder (code declares inputs by
        placeholder, the live page names them by label)."""
        def n(x) -> str:
            return re.sub(r"\s+", " ", (x or "").strip().lower())

        def outcome(ts: list[TestRecord]) -> str | None:
            done = [t for t in ts if t.status in ("pass", "fail")]
            return None if not done else ("fail" if any(t.status == "fail" for t in done) else "pass")

        wiring = [t for t in tests if t.technique == "form-wiring"]
        wired = any(t.status == "pass" for t in wiring)
        FIELD_KINDS, BUTTON_KINDS = {"input", "textarea", "select", "file", "clickable"}, {"button", "clickable"}
        targets: list[tuple[set[str], str | None, set[str]]] = []
        if wired:
            # validation results mean something only if a valid submit really goes through;
            # otherwise "nothing was sent" proves nothing about the field
            for f in form["fields"]:
                keys = {n(f.get("label")), n(f.get("placeholder"))} - {""}
                targets.append((keys, outcome([t for t in tests if f"|{f['label']}|" in t.test_key]), FIELD_KINDS))
        sub = n((form["submit"] or {}).get("name"))
        submit_tests = wiring + [t for t in tests if t.technique == "failure-feedback"]
        if sub and any(t.status in ("pass", "fail") for t in wiring):
            targets.append(({sub}, outcome(submit_tests), BUTTON_KINDS))
        path = [s.get("name") for s in st.reveal]
        for c in components:
            if c.status not in ("observed", "untested", "skipped", "not_observed"):
                continue
            here = c.page_url == st.page_url and [s.get("name") for s in (c.reveal or [])] == path
            if not here:
                continue
            names = {n(c.name), n(c.label), n((c.attrs or {}).get("placeholder"))} - {""}
            for keys, result, kinds in targets:
                if result and c.kind in kinds and names & keys:
                    c.status = result
                    c.notes = (c.notes + "; " if c.notes else "") + f"covered by form tests of '{_form_name(form)}'"
                    break


# ---------------------------------------------------------------------------
# Sign-in: real requests, no guard, at most two failed attempts
# ---------------------------------------------------------------------------

AUTH_TOKENS_JS = """() => { try {
  // Only values that hold a real credential: a JWT anywhere in the value (also inside a persisted
  // JSON store), or a long opaque string. A store that merely records {"token":null,"error":...}
  // or an attempt counter is not a session.
  const JWT = /[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}/;
  const OPAQUE = /^(Bearer )?[A-Za-z0-9_\\-+=\\/.]{24,}$/;
  return Object.keys(localStorage).filter(k => /token|session|auth|jwt/i.test(k)).map(k => {
    const v = localStorage.getItem(k) || '';
    const m = v.match(JWT);
    return m ? k + '=' + m[0] : (OPAQUE.test(v) ? k + '=' + v : null);
  }).filter(Boolean);
} catch (e) { return []; } }"""
LOGIN_PATH_RE = re.compile(r"/(login|signin|sign-in|log-in)(/|$)", re.I)


def _auth_tokens(page) -> set[str]:
    with contextlib.suppress(Exception):
        return set(page.evaluate(AUTH_TOKENS_JS))
    return set()


def _norm_path(path: str) -> str:
    return (path or "/").rstrip("/") or "/"


def _spa_signed_in(page, login_path: str = "/login", tokens_before: set[str] | None = None) -> bool:
    """Signed in, whether or not the URL changes. A router-less SPA may keep its sign-in
    screen at "/" and just swap the view, so the evidence is: the page left the sign-in
    path, a new auth token was stored, or the password field went away (and stayed away).
    A failed attempt does none of these."""
    with contextlib.suppress(Exception):
        path = urlparse(page.url).path
        if _norm_path(path) != _norm_path(login_path) and not LOGIN_PATH_RE.search(path):
            return True
    if _auth_tokens(page) - (tokens_before or set()):
        return True

    def pw_hidden() -> bool:
        with contextlib.suppress(Exception):
            pw = page.locator("input[type=password]")
            return pw.count() == 0 or not pw.first.is_visible()
        return False

    if pw_hidden():
        page.wait_for_timeout(400)  # not a re-render of the same form
        return pw_hidden()
    return False


def auth_tests(settings: Settings, live_url: str, login_url: str, email: str, password: str, events, tag: str) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    if not login_url:
        return tests, findings
    route = urlparse(login_url).path or "/login"

    def rec(key, name, expected, observed, status, cat="security"):
        t = TestRecord(test_key=f"auth|{key}", name=name, technique="auth", category=cat, route=route, expected=expected, observed=observed, status=status, reproduced_n=1, reproduced_m=1)
        tests.append(t)
        return t

    def finding(key, sev, cat, title, t, ref, conf=0.9):
        findings.append(FindingRecord(fingerprint=fingerprint("auth", key), route=route, category=cat, severity=sev, title=title, expected=t.expected, observed=t.observed,
                                      standard_ref=ref, confidence=conf, reproduced_n=1, reproduced_m=1, status=gate_status(conf), test_key=t.test_key))

    events("forms", "Sign-in tests: masking, empty submit, wrong credentials, error wording", 83)
    with BrowserSession(settings) as bs:
        ctx = bs.new_context()
        page = ctx.new_page()
        mon = attach_monitor(page)
        writes: list[tuple[str, str]] = []
        page.on("request", lambda req: writes.append((req.method, urlparse(req.url).path)) if req.method not in ("GET", "HEAD", "OPTIONS") else None)
        goto(page, login_url, settings)
        pw = page.locator("input[type=password]").first
        em = page.locator("input[type=email], input[name*=email i], input[id*=email i], input[autocomplete=username]").first
        if not pw.count() or not em.count():
            rec("form", "Sign-in form present", "Email and password inputs", "not found", "blocked")
            ctx.close()
            return tests, findings
        submit = page.locator("button[type=submit], input[type=submit]").first

        # masking + show/hide toggle
        masked = pw.get_attribute("type") == "password"
        t = rec("mask", "Password is masked by default", "Password input is type=password", f"type={pw.get_attribute('type')}", "pass" if masked else "fail")
        if not masked:
            finding("mask", "high", "security", "Password is visible while typing on the sign-in page", t, "OWASP ASVS 2.7 / WSTG-ATHN")
        toggle = page.get_by_role("button", name=re.compile(r"(show|hide|reveal|view).*password|password.*(show|hide)", re.I)).first
        pw_el = pw.element_handle()  # the type attribute changes, so pin the element itself
        if toggle.count() and pw_el is not None:
            toggle.click()
            page.wait_for_timeout(200)
            shown = pw_el.get_attribute("type")
            toggle.click()
            page.wait_for_timeout(200)
            hidden = pw_el.get_attribute("type")
            ok = shown == "text" and hidden == "password"
            t = rec("toggle", "Show/hide password toggles masking", "First click shows the password, second click hides it", f"after 1st: {shown}, after 2nd: {hidden}", "pass" if ok else "fail", cat="functional")
            if not ok:
                finding("toggle", "low", "functional", "Show/hide password control does not toggle masking", t, "Nielsen #1 Visibility of system status")

        # empty submit: nothing may be sent
        n0 = len(writes)
        em.fill("")
        pw.fill("")
        submit.click()
        page.wait_for_timeout(800)
        sent = len(writes) > n0
        t = rec("empty", "Empty sign-in is stopped before any request", "No request is sent with empty credentials", "request sent" if sent else "stopped in the browser", "fail" if sent else "pass", cat="validation")
        if sent:
            finding("empty", "low", "validation", "Sign-in sends a request with empty credentials", t, "WCAG 2.2 3.3.1 Error Identification")

        # "Remember me"-style checkbox on the sign-in form toggles (no request needed)
        goto(page, login_url, settings)
        with contextlib.suppress(Exception):
            cb = page.locator("form input[type=checkbox]").first
            if cb.count() and cb.is_visible():
                label = (page.evaluate("(el) => { const l = el.closest('label') || document.querySelector(`label[for='${el.id}']`); return ((l && l.innerText) || el.getAttribute('aria-label') || 'checkbox').trim(); }", cb.element_handle()) or "checkbox")[:40]
                before = cb.is_checked()
                cb.click()
                after = cb.is_checked()
                cb.click()
                back = cb.is_checked()
                ok_toggle = after != before and back == before
                rec("remember", f"'{label}' on the sign-in form toggles", "Clicking toggles the checkbox on and off", f"checked: {before} -> {after} -> {back}",
                    "pass" if ok_toggle else "fail", cat="functional")
                if not ok_toggle:
                    finding("remember", "low", "functional", f"'{label}' on the sign-in form does not toggle", tests[-1], "WCAG 2.2 4.1.2 Name, Role, Value")

        rl = {"hit": False}
        page.on("response", lambda r: rl.__setitem__("hit", True) if r.status == 429 else None)

        def attempt(e: str, p: str) -> dict:
            goto(page, login_url, settings)
            page.locator("input[type=email], input[name*=email i], input[id*=email i], input[autocomplete=username]").first.fill(e)
            page.locator("input[type=password]").first.fill(p)
            fb0 = page.evaluate(FEEDBACK_JS)
            tok0 = _auth_tokens(page)
            mon.reset()
            rl["hit"] = False
            page.locator("button[type=submit], input[type=submit]").first.click()
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=6000)
            page.wait_for_timeout(settings.settle_ms)
            fb1 = page.evaluate(FEEDBACK_JS)
            old = set(fb0["alerts"]) | set(fb0["errors"])
            msg = next((x for x in fb1["alerts"] + fb1["errors"] if x and x not in old), "")
            return {"path": urlparse(page.url).path, "message": msg, "errors": mon.snapshot()["page_errors"], "rate_limited": rl["hit"], "signed_in": _spa_signed_in(page, route, tok0)}

        unknown = attempt(f"qa.probe.unknown+{tag}@example.com", "Wrong#Pass-2026")
        if unknown["rate_limited"]:
            # The login is rate-limited (often by our own prior attempts); negative-auth
            # results would be unreliable, so record and stop rather than emit false findings.
            rec("ratelimited", "Negative sign-in checks", "Run negative login tests", "login endpoint returned 429 (rate limited); negative-auth checks skipped to avoid false results and deepening the lockout", "blocked", cat="security")
            return tests, findings
        stayed = not unknown["signed_in"]
        t = rec("unknown", "Unknown account is refused with a message", "Stays on sign-in and explains the failure", f"path {unknown['path']}; message {unknown['message']!r}",
                "pass" if stayed and unknown["message"] else "fail", cat="functional")
        if not stayed:
            finding("unknown-in", "critical", "security", "Sign-in accepts an unknown account", t, "OWASP WSTG-ATHN-04")
        elif not unknown["message"]:
            finding("unknown-silent", "medium", "ux", "Failed sign-in gives no message", t, "Nielsen #9 Help users recognize, diagnose, and recover from errors")
        if unknown["message"] and LEAK_RE.search(unknown["message"]):
            t2 = rec("leak", "Sign-in error does not expose internals", "Generic wording, no stack traces or internal identifiers", unknown["message"], "fail")
            finding("leak", "medium", "security", "Sign-in error message exposes internal details", t2, "OWASP WSTG-ERRH-01")
        else:
            rec("leak", "Sign-in error does not expose internals", "Generic wording, no stack traces or internal identifiers", unknown["message"] or "(no message)", "pass")

        if email:
            wrong = attempt(email, "Wrong#Pass-2026")  # the one failed attempt on the real account
            same = (wrong["message"] or "").strip().lower() == (unknown["message"] or "").strip().lower()
            t = rec("enum", "Sign-in errors do not reveal which accounts exist", "Unknown account and wrong password produce the same message",
                    f"unknown account: {unknown['message']!r}; wrong password: {wrong['message']!r}", "pass" if same else "fail")
            if not same and unknown["message"] and wrong["message"]:
                finding("enum", "medium", "security", "Sign-in error wording reveals whether an account exists", t, "OWASP WSTG-IDNT-04 Account Enumeration")

            # valid sign-in with the Enter key
            goto(page, login_url, settings)
            page.locator("input[type=email], input[name*=email i], input[id*=email i], input[autocomplete=username]").first.fill(email)
            pwl = page.locator("input[type=password]").first
            pwl.fill(password)
            tok0 = _auth_tokens(page)
            pwl.press("Enter")
            ok = False
            for _ in range(int(settings.nav_timeout_ms / 500) + 1):
                if _spa_signed_in(page, route, tok0):
                    ok = True
                    break
                page.wait_for_timeout(500)
            if rl["hit"]:
                rec("enter", "Pressing Enter in the password field signs in", "Enter submits the form", "skipped: login rate-limited (429)", "blocked", cat="functional")
            else:
                t = rec("enter", "Pressing Enter in the password field signs in", "Enter submits the form and the user is signed in",
                        f"after Enter: path {urlparse(page.url).path}, signed in={ok} (URL change, login form gone, or auth token stored)", "pass" if ok else "fail", cat="functional")
                if not ok:
                    finding("enter", "low", "functional", "Enter key does not submit the sign-in form", t, "WCAG 2.2 2.1.1 Keyboard")
        ctx.close()
    return tests, findings
