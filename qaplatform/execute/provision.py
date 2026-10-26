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

from qaplatform.execute.forms import FormTester, _form_name, _form_sig, sample_value
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint, step_of
from qaplatform.explore.safety import DESTRUCTIVE
from qaplatform.explore.scripts import FEEDBACK_JS, FIND_MARKED_JS, FORMS_JS, PAGE_STATE_JS

CATEGORY = "functional"
CREATE_RE = re.compile(r"\b(add|create|new|invite|register)\b", re.I)
DELETE_RE = re.compile(r"\b(delete|remove|destroy|purge)\b", re.I)
DATADEP_RE = re.compile(r"\b(disable|enable|deactivate|reactivate|reset|suspend|archive|restore|revoke)\b", re.I)
CONFIRM_RE = re.compile(r"\b(delete|remove|confirm|yes|ok)\b", re.I)
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
        if t == "email" or "email" in field["label"].lower():
            return f"{self.mark}{n}@qa.probe"
        base = sample_value(field, self.mark) or (self.mark + str(n))
        # ensure the marker is in the value so we can find the record later
        return f"{self.mark}{n}" if (t in ("", "text") or field["tag"] == "textarea") and "password" not in field["label"].lower() else base

    def _fill_create(self, form: dict, n: int) -> tuple[bool, str]:
        marked = False
        for f in form["fields"]:
            v = self._marked_value(f, n)
            if v is None:
                continue
            if self.mark in str(v):
                marked = True
            try:
                loc = self.r.page.locator(f["selector"]).first
                if f["tag"] == "select":
                    loc.select_option(index=1, timeout=self.r.settings.action_timeout_ms)
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
        if DELETE_RE.search(name):
            return False
        return any(f["type"] in ("", "text", "email") or f["tag"] == "textarea" for f in form["fields"])

    def _confirm_dialog(self) -> None:
        # Click the confirm button INSIDE the dialog/modal/overlay, not a same-labelled
        # button elsewhere on the page (e.g. the row's own Delete behind the modal).
        page = self.r.page
        with contextlib.suppress(Exception):
            modal = page.locator('[role=dialog], [role=alertdialog], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="overlay" i]').last
            scope = modal if modal.count() else page
            for name in (CONFIRM_RE,):
                btn = scope.get_by_role("button", name=name).last
                if btn.count() and btn.is_visible():
                    btn.click(timeout=self.r.settings.action_timeout_ms)
                    page.wait_for_timeout(self.r.settings.settle_ms)
                    return

    def _find_marked(self, value: str) -> list[dict]:
        try:
            return self.r.page.evaluate(FIND_MARKED_JS, value)
        except Exception:
            return []

    # -- main --------------------------------------------------------------
    def run(self, components: list[ComponentRecord], pages, protected_routes: list[str], login_path: str) -> tuple[list, list, dict]:
        ft = FormTester(self.r, self.events, {}, self.mark)
        with self.r.session():
            forms = ft.discover(pages, components, protected_routes, login_path)
            create_forms = [(st, f) for st, f in forms if self._is_create_form(f)]
            self.events("provision", f"Create-then-destroy on disposable data: {len(create_forms)} create form(s) found", 92)
            n = 0
            for st, form in create_forms[:4]:
                n += 1
                self._provision_one(st, form, n)
            self._teardown()
        return self.tests, self.findings, {"created": len(self.created), "cleaned": sum(1 for c in self.created if c.get("cleaned"))}

    def _provision_one(self, st, form: dict, n: int) -> None:
        name = _form_name(form)
        sig = _form_sig(form)
        where = st.route + (f" (after {' > '.join(s.get('name') or '?' for s in st.reveal)})" if st.reveal else "")
        ok, _ = self.r.prepare(st.page_url, st.reveal)
        self.r._dirty = True
        if not ok:
            return
        # reload the form fresh and fill with a marked value
        form_now = next((f for f in self.r.page.evaluate(FORMS_JS) if _form_sig(f) == sig), None)
        if not form_now:
            return
        marked, why = self._fill_create(form_now, n)
        if not marked:
            self.tests.append(TestRecord(test_key=f"provision-create|{st.route}|{name}", name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY, route=st.route,
                                         expected="A disposable, marked record is created", observed=f"skipped: {why}", status="blocked"))
            return
        value = f"{self.mark}{n}"
        # submit the create FOR REAL (only this write is un-guarded)
        before = self.r.page.evaluate(FEEDBACK_JS)
        with self.r.guard.suspended():
            try:
                self.r.page.locator(form_now["submit"]["selector"]).first.click(timeout=self.r.settings.action_timeout_ms)
            except Exception as exc:
                self.tests.append(TestRecord(test_key=f"provision-create|{st.route}|{name}", name=f"Create a demo record via '{name}'", technique="provision", category=CATEGORY, route=st.route,
                                             expected="A disposable record is created", observed=f"submit failed: {type(exc).__name__}", status="blocked"))
                return
            self.r.page.wait_for_timeout(self.r.settings.settle_ms + 800)
        # verify creation: the marker is now visible somewhere
        self.r.prepare(st.page_url, st.reveal)
        rows = self._find_marked(value)
        created_ok = bool(rows)
        ct = TestRecord(test_key=f"provision-create|{st.route}|{name}", name=f"Create a demo record via '{name}' on {where}", technique="provision", category=CATEGORY, route=st.route,
                        steps=[{"type": "FILL", "value": f"marked '{value}'"}, {"type": "CLICK", "target": form_now["submit"]}, {"type": "OBSERVE"}],
                        expected="Submitting the create form adds the marked record and it appears in the list",
                        observed=f"marked record {'found' if created_ok else 'NOT found'} after create", status="pass" if created_ok else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(ct)
        if not created_ok:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-create", st.route, name), route=st.route, category=CATEGORY, severity="high",
                                               title=f"Creating a record via '{name}' on {st.route} does not take effect", expected=ct.expected, observed=ct.observed,
                                               standard_ref="ISTQB functional (CRUD); Nielsen #1 Visibility of system status", confidence=0.85, reproduced_n=1, reproduced_m=1))
            return
        self.created.append({"value": value, "page_url": st.page_url, "reveal": st.reveal, "cleaned": False})

        # exercise the demo record's controls: data-dependent first, then destructive (which also cleans up)
        row = rows[0]
        datadep = [c for c in row["controls"] if DATADEP_RE.search(c["label"] or "")]
        destructive = [c for c in row["controls"] if DELETE_RE.search(c["label"] or "")]
        for c in datadep[:2]:
            self._test_datadep(st, value, c)
        if destructive:
            self._test_destroy(st, value, destructive[0])

    def _test_datadep(self, st, value: str, control: dict) -> None:
        # re-find the marked row to get a fresh selector, then click the control on OUR record only
        rows = self._find_marked(value)
        if not rows:
            return
        before = next((r for r in rows), {})
        state_before = before.get("text", "")
        with self.r.guard.suspended():
            try:
                self.r.page.locator(control["selector"]).first.click(timeout=self.r.settings.action_timeout_ms)
                self.r.page.wait_for_timeout(self.r.settings.settle_ms)
            except Exception:
                return
        self.r.prepare(st.page_url, st.reveal)
        rows_after = self._find_marked(value)
        changed = (not rows_after) or (rows_after and rows_after[0].get("text", "") != state_before)
        t = TestRecord(test_key=f"provision-datadep|{st.route}|{control['label']}", name=f"'{control['label']}' on the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                       expected=f"'{control['label']}' changes the demo record's state", observed=f"record state {'changed' if changed else 'unchanged'} after '{control['label']}'",
                       status="pass" if changed else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(t)
        if not changed:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-datadep", st.route, control["label"]), route=st.route, category="functional", severity="medium",
                                               title=f"'{control['label']}' on {st.route} has no effect on the record", expected=t.expected, observed=t.observed,
                                               standard_ref="ISTQB functional testing", confidence=0.8, reproduced_n=1, reproduced_m=1))

    def _test_destroy(self, st, value: str, control: dict) -> None:
        rows = self._find_marked(value)
        if not rows or self.mark not in (rows[0].get("text", "") or "").lower():
            return  # safety: only destroy a row that still carries our marker
        with self.r.guard.suspended():
            try:
                self.r.page.locator(control["selector"]).first.click(timeout=self.r.settings.action_timeout_ms)
                self.r.page.wait_for_timeout(self.r.settings.settle_ms)
            except Exception:
                return
            self._confirm_dialog()
        self.r.prepare(st.page_url, st.reveal)
        gone = not self._find_marked(value)
        t = TestRecord(test_key=f"provision-destroy|{st.route}|{control['label']}", name=f"'{control['label']}' deletes the demo record ({st.route})", technique="provision", category="functional", route=st.route,
                       steps=[{"type": "CLICK", "target": {"selector": control["selector"], "name": control["label"]}}, {"type": "CONFIRM"}, {"type": "OBSERVE"}],
                       expected=f"'{control['label']}' (with confirmation) removes the demo record",
                       observed=f"demo record {'removed' if gone else 'STILL present'} after '{control['label']}'", status="pass" if gone else "fail", reproduced_n=1, reproduced_m=1)
        self.tests.append(t)
        if gone:
            for c in self.created:
                if c["value"] == value:
                    c["cleaned"] = True
        else:
            self.findings.append(FindingRecord(fingerprint=fingerprint("provision-destroy", st.route, control["label"]), route=st.route, category="functional", severity="high",
                                               title=f"'{control['label']}' on {st.route} does not delete the record", expected=t.expected, observed=t.observed,
                                               standard_ref="ISTQB functional (CRUD delete)", confidence=0.85, reproduced_n=1, reproduced_m=1))

    def _teardown(self) -> None:
        for c in self.created:
            if c.get("cleaned"):
                continue
            with contextlib.suppress(Exception):
                self.r.prepare(c["page_url"], c["reveal"])
                rows = self._find_marked(c["value"])
                if not rows:
                    c["cleaned"] = True
                    continue
                dels = [x for x in rows[0]["controls"] if DELETE_RE.search(x["label"] or "")]
                if dels:
                    with self.r.guard.suspended():
                        self.r.page.locator(dels[0]["selector"]).first.click(timeout=self.r.settings.action_timeout_ms)
                        self.r.page.wait_for_timeout(self.r.settings.settle_ms)
                        self._confirm_dialog()
                    if not self._find_marked(c["value"]):
                        c["cleaned"] = True
        left = [c["value"] for c in self.created if not c.get("cleaned")]
        if left:
            self.events("provision", f"WARNING: {len(left)} demo record(s) could not be cleaned up: {', '.join(left)}", None, level="warn")


def run_provision(runner: SmokeRunner, components, pages, protected_routes, login_path, events, tag: str) -> tuple[list, list, dict]:
    return Provisioner(runner, events, tag).run(components, pages, protected_routes, login_path)
