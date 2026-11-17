"""Destructive actions: confirmation is required, and Cancel must change nothing.

A destructive control is never confirmed. When the code says its handler opens
a confirmation first, the control is clicked under the write guard: a write at
that point means the action ran without asking. The confirmation is then
cancelled, and a write at that point means Cancel did not cancel. When the code
shows a destructive API call with no confirmation step, that is reported from
the code alone for a human to review; the control is not clicked.
"""
from __future__ import annotations

import re

from qaplatform.execute.guard import describe
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint, step_of
from qaplatform.explore.scripts import CANCEL_IN_LAYER_JS, LAYERS_JS, PAGE_STATE_JS

GATED_RE = re.compile(r"confirm", re.I)


def run_destructive(runner: SmokeRunner, components: list[ComponentRecord], events) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    destructive = [c for c in components if c.action_class == "destructive"]
    dynamic = [c for c in destructive if "confirm" in ((c.intent or {}).get("opens") or []) and c.page_url and c.visible and (c.selector or c.role)]

    # static: a destructive API call with no confirmation step and no typed-confirmation gate
    for c in destructive:
        intent = c.intent or {}
        calls = (intent.get("api_calls") or []) + (intent.get("deferred_api_calls") or [])
        if "confirm" in (intent.get("opens") or []) or not calls:
            continue
        if GATED_RE.search(str((c.attrs or {}).get("disabled") or "")):
            continue
        api = ", ".join(f"{x['method']} {x['path']}" for x in calls[:2])
        tests.append(TestRecord(test_key=f"confirm-static|{c.key}", name=f"'{c.label}' on {c.route} asks before acting", technique="confirm-static", category="ux", route=c.route,
                                expected="A destructive action asks for confirmation before calling the API", observed=f"handler calls {api} directly (from code)", status="fail",
                                reproduced_n=1, reproduced_m=1, component_key=c.key))
        findings.append(FindingRecord(fingerprint=fingerprint("confirm-static", c.route, c.key), route=c.route, category="ux", severity="medium",
                                      title=f"'{c.label}' on {c.route} runs without a confirmation step",
                                      description=f"From the code: {c.code_file}:{c.code_line} calls {api} on click with no confirmation dialog. Not clicked by the platform.",
                                      expected="Destructive actions ask for confirmation (error prevention)", observed=f"direct call: {api}",
                                      standard_ref="Nielsen #5 Error prevention; ISO 9241-110 error tolerance", confidence=0.6, reproduced_n=0, reproduced_m=0,
                                      status="needs_review", component_key=c.key))

    if not dynamic:
        return tests, findings
    with runner.session():
        for i, c in enumerate(dynamic, 1):
            events("destructive", f"Opening and cancelling confirmation {i}/{len(dynamic)}: '{c.label}' on {c.route}", 82)
            label = c.label or c.name
            ok, why = runner.prepare(c.page_url, c.reveal)
            loc = runner.locate(c.selector, c.role, c.name or c.label) if ok else None
            base = TestRecord(test_key=f"confirm|{c.key}", name=f"'{label}' on {c.route} asks for confirmation and Cancel changes nothing", technique="confirm-guard",
                              category="ux", route=c.route, expected="Clicking opens a confirmation without sending anything; Cancel closes it without sending anything",
                              steps=[{"type": "NAVIGATE", "value": c.page_url}] + [{"type": "CLICK", "target": s} for s in c.reveal] + [{"type": "CLICK", "target": step_of(c)}, {"type": "CLICK", "target": {"name": "Cancel"}}],
                              component_key=c.key)
            if loc is None:
                base.status, base.observed = "blocked", why or "control not found"
                tests.append(base)
                continue
            layers_before = {l["signature"] for l in runner.page.evaluate(LAYERS_JS)}
            st_before = runner.page.evaluate(PAGE_STATE_JS)
            mark = runner.guard.mark()
            try:
                loc.click(timeout=runner.settings.action_timeout_ms)
            except Exception as exc:
                base.status, base.observed = "blocked", f"not clickable: {type(exc).__name__}"
                tests.append(base)
                continue
            runner.page.wait_for_timeout(runner.settings.settle_ms)
            runner._dirty = True
            writes = runner.guard.since(mark)
            layers_after = runner.page.evaluate(LAYERS_JS)
            st_after = runner.page.evaluate(PAGE_STATE_JS)
            new_layers = [l for l in layers_after if l["signature"] not in layers_before and not l["isMenu"]]
            dialog_opened = bool(new_layers) or st_after.get("dialogs", 0) > st_before.get("dialogs", 0) or bool(runner.mon.dialogs)
            if writes:
                base.status, base.observed = "fail", f"write sent on first click (intercepted): {describe(writes)}"
                shot = runner.screenshot(f"noconfirm_{c.route}_{label}")
                findings.append(FindingRecord(fingerprint=fingerprint("confirm-missing", c.route, c.key), route=c.route, category="ux", severity="high",
                                              title=f"'{label}' on {c.route} acts without asking for confirmation", expected=base.expected, observed=base.observed,
                                              standard_ref="Nielsen #5 Error prevention", confidence=0.9, reproduced_n=1, reproduced_m=1,
                                              evidence=[x for x in [shot] if x], component_key=c.key, test_key=base.test_key))
                c.status = "fail"
                tests.append(base)
                continue
            if not dialog_opened:
                base.status, base.observed = "fail", "no confirmation appeared and nothing was sent"
                findings.append(FindingRecord(fingerprint=fingerprint("confirm-none", c.route, c.key), route=c.route, category="functional", severity="medium",
                                              title=f"'{label}' on {c.route} does nothing (its confirmation never opens)", expected=base.expected, observed=base.observed,
                                              standard_ref="Nielsen #1 Visibility of system status", confidence=0.8, reproduced_n=1, reproduced_m=1, component_key=c.key, test_key=base.test_key))
                c.status = "fail"
                tests.append(base)
                continue
            cancel = None
            for layer in new_layers or layers_after:
                cancel = runner.page.evaluate(CANCEL_IN_LAYER_JS, layer["selector"])
                if cancel:
                    break
            if not cancel:
                base.status, base.observed = "fail", "confirmation opened but offers no Cancel"
                findings.append(FindingRecord(fingerprint=fingerprint("confirm-nocancel", c.route, c.key), route=c.route, category="ux", severity="medium",
                                              title=f"Confirmation for '{label}' on {c.route} has no way to cancel", expected=base.expected, observed=base.observed,
                                              standard_ref="Nielsen #3 User control and freedom", confidence=0.8, reproduced_n=1, reproduced_m=1, component_key=c.key, test_key=base.test_key))
                c.status = "fail"
                tests.append(base)
                continue
            mark2 = runner.guard.mark()
            try:
                runner.page.locator(cancel["selector"]).first.click(timeout=runner.settings.action_timeout_ms)
            except Exception:
                runner.page.get_by_role("button", name=cancel["name"], exact=True).first.click(timeout=runner.settings.action_timeout_ms)
            runner.page.wait_for_timeout(runner.settings.settle_ms)
            writes2 = runner.guard.since(mark2)
            still = {l["signature"] for l in runner.page.evaluate(LAYERS_JS)}
            closed = not any(l["signature"] in still for l in new_layers)
            if writes2:
                base.status, base.observed = "fail", f"'{cancel['name']}' sent a write (intercepted): {describe(writes2)}"
                findings.append(FindingRecord(fingerprint=fingerprint("confirm-cancel-writes", c.route, c.key), route=c.route, category="functional", severity="critical",
                                              title=f"Cancelling '{label}' on {c.route} still performs the action", expected=base.expected, observed=base.observed,
                                              standard_ref="Nielsen #3 User control and freedom", confidence=0.9, reproduced_n=1, reproduced_m=1, component_key=c.key, test_key=base.test_key))
                c.status = "fail"
            elif not closed:
                base.status, base.observed = "fail", f"'{cancel['name']}' did not close the confirmation"
                findings.append(FindingRecord(fingerprint=fingerprint("confirm-cancel-open", c.route, c.key), route=c.route, category="functional", severity="medium",
                                              title=f"Cancel does not close the confirmation for '{label}' on {c.route}", expected=base.expected, observed=base.observed,
                                              standard_ref="Nielsen #3 User control and freedom", confidence=0.85, reproduced_n=1, reproduced_m=1, component_key=c.key, test_key=base.test_key))
                c.status = "fail"
            else:
                base.status = "pass"
                base.observed = f"confirmation opened, nothing sent; '{cancel['name']}' closed it, nothing sent"
                c.status = "pass"
                c.notes = "confirmation opened and cancelled under the write guard; never confirmed"
            base.reproduced_n, base.reproduced_m = 1, 1
            tests.append(base)
    return tests, findings
