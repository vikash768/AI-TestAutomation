"""Create-then-destroy on disposable data (opt-in; off by default).

Destructive and data-dependent controls can't be tested safely on real records:
deleting a real user, or finding a per-row Reset button when there are no rows,
is not acceptable on a live system. This module instead CREATES a clearly
marked demo record, exercises the control on THAT record, verifies the outcome,
and cleans the record up.

Safety invariants:
- Every write is blocked by the guard except the specific create / toggle /
  delete actions this module performs on its own records, which it un-guards
  one at a time.
- A destructive or data-dependent control is clicked ONLY when it sits inside a
  container whose visible text carries this run's unique marker — i.e. a record
  this module just created. Real records are never touched.
- A teardown pass removes any marked record left behind.

It runs only when explicitly enabled and the operator has authorised real
writes against disposable data on the target.
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path

from qaplatform.execute.forms import FormTester, _form_name, _form_sig, sample_value
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint, step_of
from qaplatform.explore.safety import DESTRUCTIVE
import hashlib

from qaplatform.explore.scripts import FEEDBACK_JS, FORMS_JS, LAYER_COUNT_JS, LIST_READY_JS, MARK_CONTROL_JS, MARKED_ROWS_JS, PAGE_STATE_JS

CATEGORY = "functional"
CREATE_RE = re.compile(r"\b(add|create|new|invite|register|upload)\b", re.I)
# Never clicked, even on a demo record: these push data outward (a published firmware is
# offered to real devices; a release/deploy reaches users). Reported as not exercised instead.
OUTWARD_RE = re.compile(r"\b(publish|release|deploy|push|roll ?out|go live|send to|broadcast|activate firmware)\b", re.I)
ROW_DELETE_JS = """() => Array.from(document.querySelectorAll('tr, li, [role=row], [role=listitem]')).some(row =>
  Array.from(row.querySelectorAll('button, [role=button], a')).some(b => /\\b(delete|remove|destroy|purge)\\b/i.test((b.innerText || b.getAttribute('aria-label') || '').trim())))"""


def _norm(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


SUCCESS_RE = re.compile(r"\b(uploaded|created|saved|added|success|successfully|can now sign in)\b", re.I)
VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?$")
DELETE_RE = re.compile(r"\b(delete|remove|destroy|purge)\b", re.I)
DATADEP_RE = re.compile(r"\b(disable|enable|deactivate|reactivate|reset|suspend|archive|restore|revoke)\b", re.I)
# Exact confirm labels only, and only inside a dialog that appeared after OUR click.
CONFIRM_EXACT_RE = re.compile(r"^(delete|remove|confirm|yes|ok|yes,? delete( it)?|delete (it|record|permanently))$", re.I)
CANCEL_RE = re.compile(r"\b(cancel|no|keep|close)\b", re.I)


class Provisioner:
    def __init__(self, runner: SmokeRunner, events, tag: str):
        self.r = runner
        self.events = events
        self.mark = "qaprobe" + re.sub(r"[^a-z0-9]", "", tag.lower())[:8]
        self.created: list[dict] = []   # {value, page_url, reveal}
        self.tests: list[TestRecord] = []
        self.findings: list[FindingRecord] = []

    # -- helpers -----------------------------------------------------------
    def _marked_value(self, field: dict, n: int) -> str:
        t = field["type"]
        if t == "file":
            return self._demo_file(n)
        if "version" in (field.get("label") or "").lower() or VERSION_RE.match(field.get("placeholder") or ""):
            # valid, deliberately low (never the "highest" release), and unique per run
            return f"0.0.{int(hashlib.sha1(self.mark.encode()).hexdigest()[:6], 16) % 90000 + 10000}{n}"
        if t == "email" or "email" in field["label"].lower():
            return f"{self.mark}{n}@qa.probe"
        base = sample_value(field, self.mark) or (self.mark + str(n))
        # ensure the marker is in the value so we can find the record later
        return f"{self.mark}{n}" if (t in ("", "text") or field["tag"] == "textarea") and "password" not in field["label"].lower() else base

    def _deletable(self, form: dict) -> tuple[bool, str]:
        """Can a record made by this form be deleted again? Evidence from the code (a Delete-type
        control declared in the same screen file as the form's submit button) or from the live
        list (a Delete-type control on an existing row)."""
        sub = _norm((form.get("submit") or {}).get("name"))
        comps = getattr(self, "components", [])
        here = getattr(self, "_screen_url", None)
        # the submit button as matched on THIS screen (a generic "Save" elsewhere is no evidence)
        files = {c.code_file for c in comps if c.code_file and sub and _norm(c.name or c.label) == sub and c.page_url == here}
        # inputs are declared in code by their placeholder ("0.2.15"): specific placeholders tie the form to one file
        hints = {_norm(f.get("placeholder")) for f in form.get("fields") or [] if len(_norm(f.get("placeholder"))) >= 4}
        by_hint = {c.code_file for c in comps if c.code_file and hints and _norm(c.label) in hints}
        if len(by_hint) == 1:
            files |= by_hint
        for f in files:
            if any(c.code_file == f and DELETE_RE.search(c.label or "") for c in self.components):
                return True, f"Delete control declared in {f}"
        with contextlib.suppress(Exception):
            if self.r.page.evaluate(ROW_DELETE_JS):
                return True, "existing rows have a Delete control"
        where = ", ".join(sorted(files)) or "this screen"
        return False, f"no Delete/Remove control for these records in {where} or on the live list"

    def _demo_file(self, n: int) -> str:
        """A tiny, inert file for upload fields, named and filled with the marker."""
        path = Path(self.r.run_dir) / f"{self.mark}{n}.bin"
        path.write_bytes(f"QA PROBE {self.mark}{n} - disposable test upload, not a real firmware image\n".encode())
        return str(path)

    def _fill_create(self, form: dict, n: int) -> tuple[bool, str]:
        marked = False
        for f in form["fields"]:
            v = self._marked_value(f, n)
            if v is None:
                continue
            if self.mark in str(v) and f["type"] != "file":
                marked = True
            try:
                loc = self.r.page.locator(f["selector"]).first
                if f["tag"] == "select":
                    loc.select_option(index=1, timeout=self.r.settings.action_timeout_ms)
                elif f["type"] == "file":
                    loc.set_input_files(v, timeout=self.r.settings.action_timeout_ms)
                    continue  # the file name carries the marker, but the record is found by its text field
                else:
                    loc.fill(v, timeout=self.r.settings.action_timeout_ms)
            except Exception:
                return False, f"could not fill '{f['label']}'", 
        return (marked, "no field carries the marker" if not marked else "")

    def _is_create_form(self, form: dict) -> bool:
        if not form["submit"] or not form["fields"]:
            return False
        name = (form["submit"]["name"] or "") + " " + (form.get("text") or "")
        if not CREATE_RE.search(name):
            return False
        # outward/destructive intent is judged from what the form IS (submit label, heading),
        # not its help text ("Publish it when..." on an upload form)
        action = f"{form['submit']['name'] or ''} {form.get('heading') or ''}"
        if DELETE_RE.search(action) or OUTWARD_RE.search(action):
            return False
        return any(f["type"] in ("", "text", "email") or f["tag"] == "textarea" for f in form["fields"])

    def _find_marked(self, value: str) -> list[dict]:
        try:
            return self.r.page.evaluate(MARKED_ROWS_JS, value)
        except Exception:
            return []

    def _find_marked_retry(self, st, value: str, tries: int = 3) -> list[dict]:
        """The list may still be loading or re-sorting; look again before concluding."""
        for i in range(tries):
            rows = self._find_marked(value)
            if rows:
                return rows
            self.r.page.wait_for_timeout(1200)
            if i < tries - 1:
                self.r.prepare(st["page_url"] if isinstance(st, dict) else st.page_url, st["reveal"] if isinstance(st, dict) else st.reveal)
        return []

    def _confirm_absent(self, page_url: str, reveal: list, value: str) -> tuple[bool, str]:
        """True only when the screen reopened, its record list really rendered, and the demo
        record is not in it. A failed reopen, an error state or a loading list proves nothing."""
        why = "not checked"
        for _ in range(3):
            ok, why = self.r.prepare(page_url, reveal)
            if not ok:
                self.r.page.wait_for_timeout(1500)
                continue
            try:
                ready = self.r.page.evaluate(LIST_READY_JS)
                rows = self.r.page.evaluate(MARKED_ROWS_JS, value)
            except Exception as exc:
                why = type(exc).__name__
                continue
            if rows:
                return False, "still present"
            if ready.get("ok"):
                return True, ""
            why = ready.get("why") or "list not ready"
            self.r.page.wait_for_timeout(1500)
        return False, f"deletion unverified ({why})"

    def _click_in_record(self, value: str, label: str, destroy: bool = False) -> tuple[bool, str]:
        """Click the control called `label` inside the ONE record carrying `value`, by identity.
        For a destructive click, a confirmation is accepted only inside a dialog that appeared
        after this click (or a native confirm), never anywhere else on the page."""
        page = self.r.page
        try:
            nonce = page.evaluate(MARK_CONTROL_JS, {"marker": value, "label": label})
        except Exception as exc:
            return False, f"could not locate: {type(exc).__name__}"
        if not nonce or str(nonce).startswith("!"):
            return False, f"not unambiguous ({str(nonce)[1:] if nonce else 'nothing found'})"
        target = page.locator(f'[data-qaprobe-target="{nonce}"]')
        layers_before = page.evaluate(LAYER_COUNT_JS)
        if destroy:
            self.r.mon.accept_confirm = True
        try:
            with self.r.guard.suspended():
                target.click(timeout=self.r.settings.action_timeout_ms)
                page.wait_for_timeout(self.r.settings.settle_ms)
        except Exception as exc:
            return False, f"click failed: {type(exc).__name__}"
        finally:
            self.r.mon.accept_confirm = False
        if destroy and page.evaluate(LAYER_COUNT_JS) > layers_before:
            layer = page.locator('[role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open], [class*="modal" i]').last
            btns = [b for b in layer.get_by_role("button").all() if b.is_visible() and CONFIRM_EXACT_RE.match((b.inner_text() or "").strip())]
            if len(btns) != 1:
                with contextlib.suppress(Exception):
                    page.keyboard.press("Escape")
                return False, "confirmation dialog without one clear confirm button; cancelled"
            with self.r.guard.suspended():
                btns[0].click(timeout=self.r.settings.action_timeout_ms)
                page.wait_for_timeout(self.r.settings.settle_ms)
        return True, ""

    # -- main --------------------------------------------------------------
    def run(self, components: list[ComponentRecord], pages, protected_routes: list[str], login_path: str) -> tuple[list, list, dict]:
        self.components = components
        ft = FormTester(self.r, self.events, {}, self.mark)
        with self.r.session():
            try:
                forms = ft.discover(pages, components, protected_routes, login_path)
                create_forms = [(st, f) for st, f in forms if self._is_create_form(f)]
                self.events("provision", f"Create-then-destroy on disposable data: {len(create_forms)} create form(s) found", 92)
                for n, (st, form) in enumerate(create_forms[:4], 1):
                    try:
                        self._provision_one(st, form, n)
                    except Exception as exc:
                        self.events("provision", f"'{_form_name(form)}' stopped: {type(exc).__name__}: {str(exc)[:120]}", None, level="warn")
            finally:
                self._teardown()  # always, whatever happened above
        return self.tests, self.findings, {"created": len(self.created), "cleaned": sum(1 for c in self.created if c.get("cleaned"))}

    def _provision_one(self, st, form: dict, n: int) -> None:
        name = _form_name(form)
        sig = _form_sig(form)
        where = st.route + (f" (after {' > '.join(s.get('name') or '?' for s in st.reveal)})" if st.reveal else "")
        key = f"provision-create|{st.route}|{name}"
        ok, _ = self.r.prepare(st.page_url, st.reveal)
        self.r._dirty = True
        if not ok:
            return
        form_now = next((f for f in self.r.page.evaluate(FORMS_JS) if _form_sig(f) == sig), None)
        if not form_now:
            return
        self._screen_url = st.page_url
        can_delete, how = self._deletable(form_now)
        if not can_delete:
            # Creating a record the app cannot remove would leave it on the live system for good.
            self.tests.append(TestRecord(test_key=key, name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY,
                                         route=st.route, expected="A disposable, marked record is created, exercised and deleted",
                                         observed=f"not created: {how}; a demo record would stay on the live system permanently", status="blocked"))
            self.events("provision", f"'{name}': not created — {how}", None)
            return
        marked, why = self._fill_create(form_now, n)
        if not marked:
            self.tests.append(TestRecord(test_key=key, name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY, route=st.route,
                                         expected="A disposable, marked record is created", observed=f"skipped: {why}", status="blocked"))
            return
        value = f"{self.mark}{n}"
        before = self.r.page.evaluate(FEEDBACK_JS)
        # submit the create FOR REAL (only this write is un-guarded); register it for teardown at once
        with self.r.guard.suspended():
            try:
                self.r.page.locator(form_now["submit"]["selector"]).first.click(timeout=self.r.settings.action_timeout_ms)
            except Exception as exc:
                self.tests.append(TestRecord(test_key=key, name=f"Create a demo record via '{name}'", technique="provision", category=CATEGORY, route=st.route,
                                             expected="A disposable record is created", observed=f"submit failed: {type(exc).__name__}", status="blocked"))
                return
            self.r.page.wait_for_timeout(self.r.settings.settle_ms + 800)
        entry = {"value": value, "page_url": st.page_url, "reveal": st.reveal, "cleaned": False, "form": name}
        self.created.append(entry)
        after = self.r.page.evaluate(FEEDBACK_JS)
        old = set(before["alerts"] + before["toasts"] + before["errors"] + before.get("successes", []))
        successes = [t for t in after.get("successes", []) if t not in old]
        new_msgs = [t for t in after["alerts"] + after["toasts"] + after["errors"] if t and t not in old]
        rejected = [t for t in new_msgs if t not in successes and not SUCCESS_RE.search(t)]
        ok_prep, _ = self.r.prepare(st.page_url, st.reveal)
        rows = self._find_marked_retry(st, value) if ok_prep else []
        if not rows and rejected and not successes and ok_prep:
            # The app refused our demo values: recorded, not a bug. The entry stays registered,
            # so teardown still looks for it and warns if it turns out to exist.
            self.tests.append(TestRecord(test_key=key, name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY, route=st.route,
                                         expected="A disposable, marked record is created", observed=f"the app rejected the demo values: {rejected[0][:140]!r}", status="blocked"))
            return
        ct = TestRecord(test_key=key, name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY, route=st.route,
                        steps=[{"type": "FILL", "value": f"marked '{value}'"}, {"type": "CLICK", "target": form_now["submit"]}, {"type": "OBSERVE"}],
                        expected="Submitting the create form adds the marked record and it appears in the list",
                        observed=f"marked record {'found' if rows else 'NOT found (looked 3 times)'} after create", status="pass" if rows else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(ct)
        if not rows:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-create", st.route, name), route=st.route, category=CATEGORY, severity="high",
                                               title=f"Creating a record via '{name}' on {st.route} does not take effect", expected=ct.expected, observed=ct.observed,
                                               standard_ref="ISTQB functional (CRUD); Nielsen #1 Visibility of system status", confidence=0.6, reproduced_n=1, reproduced_m=1,
                                               status="needs_review"))
            return

        labels = [c["label"] for c in rows[0]["controls"]]
        for lab in labels:
            if OUTWARD_RE.search(lab):
                self.tests.append(TestRecord(test_key=f"provision-outward|{st.route}|{lab}", name=f"'{lab}' on the demo record ({st.route})", technique="provision",
                                             category="functional", route=st.route, expected=f"'{lab}' works on a record",
                                             observed="not exercised: it pushes data outward (e.g. offers firmware to real devices); verify manually on a non-production system", status="blocked"))
        datadep = [lab for lab in labels if DATADEP_RE.search(lab) and not OUTWARD_RE.search(lab) and not DELETE_RE.search(lab)]
        destructive = [lab for lab in labels if DELETE_RE.search(lab) and not OUTWARD_RE.search(lab)]
        for lab in datadep[:2]:
            self._test_datadep(st, value, lab)
        if destructive:
            self._test_destroy(st, value, destructive[0])

    def _test_datadep(self, st, value: str, label: str) -> None:
        rows = self._find_marked(value)
        if len(rows) != 1:
            return
        state_before = rows[0].get("text", "")
        forms_before = len(self.r.page.evaluate(FORMS_JS))
        ok, why = self._click_in_record(value, label)
        tkey = f"provision-datadep|{st.route}|{label}"
        if not ok:
            self.tests.append(TestRecord(test_key=tkey, name=f"'{label}' on the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                                         expected=f"'{label}' changes the demo record's state", observed=f"not clicked: {why}", status="blocked"))
            return
        if len(self.r.page.evaluate(FORMS_JS)) > forms_before:
            # It opened a form (e.g. "Reset password" asks for the new value): the click works;
            # the form itself is covered by the forms module. Close it without submitting.
            with contextlib.suppress(Exception):
                self.r.page.keyboard.press("Escape")
            self.tests.append(TestRecord(test_key=tkey, name=f"'{label}' on the demo record ({st.route})", technique="provision",
                                         category="functional", route=st.route, expected=f"'{label}' responds for the demo record",
                                         observed="opened its form for the demo record (closed without submitting)", status="pass", reproduced_n=1, reproduced_m=1))
            self.r.prepare(st.page_url, st.reveal)
            return
        self.r.prepare(st.page_url, st.reveal)
        rows_after = self._find_marked_retry(st, value)
        changed = bool(rows_after) and rows_after[0].get("text", "") != state_before
        t = TestRecord(test_key=tkey, name=f"'{label}' on the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                       expected=f"'{label}' changes the demo record's state", observed=f"record state {'changed' if changed else 'unchanged'} after '{label}'",
                       status="pass" if changed else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(t)
        if not changed and rows_after:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-datadep", st.route, label), route=st.route, category="functional", severity="medium",
                                               title=f"'{label}' on {st.route} has no effect on the record", expected=t.expected, observed=t.observed,
                                               standard_ref="ISTQB functional testing", confidence=0.8, reproduced_n=1, reproduced_m=1))

    def _test_destroy(self, st, value: str, label: str) -> None:
        if len(self._find_marked(value)) != 1:
            return
        ok, why = self._click_in_record(value, label, destroy=True)
        tkey = f"provision-destroy|{st.route}|{label}"
        if not ok:
            self.tests.append(TestRecord(test_key=tkey, name=f"'{label}' deletes the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                                         expected=f"'{label}' removes the demo record", observed=f"not clicked: {why}", status="blocked"))
            return
        gone, why_absent = self._confirm_absent(st.page_url, st.reveal, value)
        if not gone and why_absent.startswith("deletion unverified"):
            self.tests.append(TestRecord(test_key=tkey, name=f"'{label}' deletes the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                                         expected=f"'{label}' removes the demo record", observed=why_absent, status="blocked"))
            return  # stays registered: teardown retries and warns if it cannot confirm
        t = TestRecord(test_key=tkey, name=f"'{label}' deletes the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                       steps=[{"type": "CLICK", "target": {"name": label}, "description": "inside the marked demo record"}, {"type": "CONFIRM"}, {"type": "OBSERVE"}],
                       expected=f"'{label}' (with any confirmation) removes the demo record",
                       observed=f"demo record {'removed' if gone else 'STILL present'} after '{label}'", status="pass" if gone else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(t)
        if gone:
            for c in self.created:
                if c["value"] == value:
                    c["cleaned"] = True
        else:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-destroy", st.route, label), route=st.route, category="functional", severity="high",
                                               title=f"'{label}' on {st.route} does not delete the record", expected=t.expected, observed=t.observed,
                                               standard_ref="ISTQB functional (CRUD delete)", confidence=0.85, reproduced_n=1, reproduced_m=1))

    def _teardown(self) -> None:
        for c in self.created:
            if c.get("cleaned"):
                continue
            with contextlib.suppress(Exception):
                absent, _ = self._confirm_absent(c["page_url"], c["reveal"], c["value"])
                if absent:
                    c["cleaned"] = True  # verified: the list rendered and the record is not in it
                    continue
                rows = self._find_marked(c["value"])
                if not rows:
                    continue  # unverified: NOT assumed gone; reported below for manual cleanup
                dels = [x["label"] for x in rows[0]["controls"] if DELETE_RE.search(x["label"]) and not OUTWARD_RE.search(x["label"])]
                if dels:
                    ok, _ = self._click_in_record(c["value"], dels[0], destroy=True)
                    if ok and self._confirm_absent(c["page_url"], c["reveal"], c["value"])[0]:
                        c["cleaned"] = True
        left = [f"{c['value']} (via '{c.get('form', '?')}')" for c in self.created if not c.get("cleaned")]
        if left:
            self.events("provision", f"WARNING: {len(left)} demo record(s) may remain and need manual cleanup: {', '.join(left)}", None, level="warn")


def run_provision(runner: SmokeRunner, components, pages, protected_routes, login_path, events, tag: str) -> tuple[list, list, dict]:
    return Provisioner(runner, events, tag).run(components, pages, protected_routes, login_path)
