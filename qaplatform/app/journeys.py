"""End-to-end journeys with QA test data on a native app.

    plan        deterministic, from the code: every screen with text fields and a submit control gets a
                create journey (QA values in, submit, reach the screen the code navigates to) and boundary
                cases from the code's own rules (required, maxLength); the LLM may add more (app/ai.py)
    validate    the safety layer, authoritative: unknown labels, sign-out, leaving the app, bulk actions
                (sync all, upload all, clear) and deletes of anything this run did not create are refused;
                each step's effect (local draft / local record / remote) is taken from the code, and the
                LLM can only make it stricter, never looser
    run         on the device, through the explorer's navigation; a control that changes data is tapped
                only when the form holds this run's values or the screen is this run's record; screens that
                need hardware (reader, Bluetooth, USB) end the journey as blocked, with the reason
    cleanup     restore overwritten fields, delete this run's records where the app offers it, report the rest

Oracles from the code (required, maxLength, a navigation target) decide pass / fail. Expectations that only
the LLM supplied end as needs_review when they are not met: they are never reported as bugs on their own.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from qaplatform.app.code import NAV_RE
from qaplatform.app.device import AppDevice, Snapshot
from qaplatform.app.explore import CLICK_ROLES, AppExplorer, Screen, similar
from qaplatform.app.testdata import QADataRegistry, owned_screen, row_owner
from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.explore.crawler import ObservedElement
from qaplatform.explore.safety import classify_action
from qaplatform.ingest.js_parser import CodeElement, CodeInventory
from qaplatform.judge.gate import gate_status
from qaplatform.reconcile.matcher import normalize

logger = logging.getLogger(__name__)

ACTIONS = ("open", "type", "tap", "submit", "verify_text", "verify_absent", "relaunch", "back")
SUBMIT_RE = re.compile(r"\b(continue|next|save|submit|create|add|done|confirm|update|apply|finish|proceed|register|ok)\b", re.I)
BULK_RE = re.compile(r"\b(all|sync|upload|import|export|backup|restore|clear|everything|reset|retry uploads?)\b", re.I)
HARDWARE_RE = re.compile(r"(connect (a |the |your )?(reader|device|sensor|meter|printer)|bluetooth|\busb\b|\botg\b|\bnfc\b|plug (it )?in|power on your|searching for (devices|readers)|no (device|reader)s? (found|connected|available))", re.I)
ERROR_TEXT_RE = re.compile(r"(required|invalid|must |use \d|at least|at most|too (short|long)|not (valid|allowed)|error|please (enter|fill|provide|select)|cannot be (empty|blank)|letters|characters)", re.I)
DRAFT_CALL_RE = re.compile(r"draft", re.I)
RECORD_CALL_RE = re.compile(r"\b(insert|save|create|record|persist|store|add|upsert|enqueue|queue|finish|complete)\w*\s*\(", re.I)
SYNC_CALL_RE = re.compile(r"\b(sync|upload|post|put|patch)\w*", re.I)
BACKGROUND_SYNC_RE = re.compile(r"(setInterval|AppState\.addEventListener|NetInfo\.addEventListener|BackgroundFetch|registerTaskAsync)")
NOTE_RE = re.compile(r"\b(note|comment|description|remark|message)s?\b", re.I)
NUMERIC_KB = {"numeric", "number-pad", "decimal-pad", "phone-pad"}
EFFECT_ORDER = ["none", "navigation", "local_draft", "local_record", "remote"]
CONFIRM_RE = re.compile(r"^(delete|remove|yes|ok|confirm|continue)\b", re.I)


@dataclass
class Step:
    action: str
    screen: str = ""
    target: str = ""
    value: str = ""
    expect: str = ""  # navigates:<route> | rejected | accepted | maxlen:<n> | changed | <empty>
    effect: str = ""
    why: str = ""
    oracle: str = "code"  # code | ai: who said what should happen


@dataclass
class Journey:
    name: str
    kind: str  # create | update | delete | validation | flow
    source: str  # code | ai
    steps: list[Step] = field(default_factory=list)
    rationale: str = ""
    blocked: str = ""  # set by validate(): why it cannot run as planned
    code_ref: str = ""


@dataclass
class JourneyResult:
    journey: Journey
    status: str = "blocked"
    done: int = 0
    reason: str = ""
    log: list[dict] = field(default_factory=list)
    qa_ids: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    findings: list[FindingRecord] = field(default_factory=list)
    actions: int = 0
    held: list[dict] = field(default_factory=list)


# -- what a step does, from the code ---------------------------------------------------------------------
def background_sync(repo: Path | None) -> bool:
    """The app uploads stored records by itself (a timer or app-state listener that syncs)."""
    if repo is None:
        return False
    for p in list(Path(repo).rglob("*.ts")) + list(Path(repo).rglob("*.tsx")) + list(Path(repo).rglob("*.js")):
        if "node_modules" in p.parts or any(part in ("android", "ios", "__tests__") for part in p.parts):
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if BACKGROUND_SYNC_RE.search(src) and re.search(r"\b(sync|upload)\w*\s*\(", src, re.I):
            return True
    return False


def code_effect(el: CodeElement | None, inv: CodeInventory, repo: Path | None, bg_sync: bool) -> tuple[str, str]:
    """(effect, why) of activating this control, read from its handler and the screen it opens."""
    if el is None:
        return "none", "no code declaration"
    writes = el.attrs.get("writes") or []
    calls = " ".join(el.calls or [])
    if writes:
        return "remote", f"its handler sends {'/'.join(writes)} ({el.file}:{el.line})"
    if SYNC_CALL_RE.search(calls) and re.search(r"\b(sync|upload)", calls, re.I):
        return "remote", f"its handler calls {re.search(r'(sync|upload)\w*', calls, re.I).group(0)} ({el.file}:{el.line})"
    effect, why = ("navigation", "navigates") if el.navigates_to else ("none", "")
    if DRAFT_CALL_RE.search(calls):
        effect, why = "local_draft", f"its handler saves a draft ({el.file}:{el.line})"
    elif any(RECORD_CALL_RE.search(c + "(") and not DRAFT_CALL_RE.search(c) for c in el.calls or []):
        effect, why = "local_record", f"its handler stores data ({el.file}:{el.line})"
    for dest in el.navigates_to:
        f = inv.page_files.get(dest)
        if repo is None or not f or not (Path(repo) / f).is_file():
            continue
        src = (Path(repo) / f).read_text(encoding="utf-8", errors="replace")
        if re.search(r"\b(sync|upload)\w*\s*\(", src, re.I) and re.search(r"\bfetch|api\.|client\.|request\(", src):
            return "remote", f"{dest} ({f}) uploads when it opens"
        if [m for m in RECORD_CALL_RE.finditer(src) if not DRAFT_CALL_RE.search(m.group(0)) and not re.match(r"(add|create)(Event|Listener|Style|Ref|Context|Store|Navigator)", m.group(0))]:
            if bg_sync:
                return "remote", f"{dest} ({f}) stores a record, and the app uploads stored records in the background"
            effect, why = "local_record", f"{dest} ({f}) stores a record"
    return effect, why


def screen_effect(route: str, inv: CodeInventory, repo: Path | None, bg_sync: bool) -> tuple[str, str]:
    """For a control the code does not name (a dynamic label): the strictest effect of anywhere its screen can navigate."""
    f = inv.page_files.get(route)
    if repo is None or not f or not (Path(repo) / f).is_file():
        return "none", ""
    src = (Path(repo) / f).read_text(encoding="utf-8", errors="replace")
    targets = list(dict.fromkeys(a or b for a, b in NAV_RE.findall(src)))
    best = ("none", "")
    for t in targets:
        eff = code_effect(CodeElement(file=f, line=0, tag="", kind="button", navigates_to=[t]), inv, repo, bg_sync)
        if stricter(eff[0], best[0]) == eff[0] and eff[0] != best[0]:
            best = (eff[0], f"{route} can go on to {eff[1] or t}")
    return best


def stricter(a: str, b: str) -> str:
    return a if EFFECT_ORDER.index(a if a in EFFECT_ORDER else "none") >= EFFECT_ORDER.index(b if b in EFFECT_ORDER else "none") else b


# -- planning --------------------------------------------------------------------------------------------
def _editable(e: CodeElement) -> bool:
    ro = e.attrs.get("readOnly") or e.attrs.get("editable") in (False, "false")
    return e.kind == "input" and not e.attrs.get("secure") and not ro and e.attrs.get("readOnly") not in (True, "true", "{true}")


def _value_for(e: CodeElement) -> str:
    if str(e.attrs.get("keyboardType") or "") in NUMERIC_KB:
        return "{number}"
    if NOTE_RE.search(e.label or ""):
        return "{qa_note}"
    return "{qa}"


def plan_from_code(inv: CodeInventory, observed_routes: set[str]) -> list[Journey]:
    out: list[Journey] = []
    for r in inv.routes:
        if not r.protected and re.search(r"login|sign|auth|pin|lock|otp|password", r.path, re.I):
            continue  # sign-in and PIN screens have their own tests and hold real secrets
        els = [e for e in inv.elements if e.route == r.path]
        fields = [e for e in els if _editable(e) and e.label]
        submit = next((e for e in els if e.kind == "button" and e.label and SUBMIT_RE.search(e.label) and (e.navigates_to or e.handler)), None)
        if not fields or submit is None:
            continue
        ref = f"{submit.file}:{submit.line}"
        steps = [Step("open", r.path, why="screen with a form")]
        steps += [Step("type", r.path, f.label, _value_for(f), why="QA value") for f in fields[:5]]
        expect = f"navigates:{submit.navigates_to[0]}" if submit.navigates_to else "accepted"
        steps.append(Step("submit", r.path, submit.label, expect=expect, why=f"the code's submit ({ref})"))
        out.append(Journey(name=f"Fill and submit {r.path} with QA data", kind="create", source="code", steps=steps, code_ref=ref,
                           rationale=f"{len(fields)} field(s) and '{submit.label}' on {r.path}"))
        for f in fields[:5]:
            mx = f.attrs.get("maxLength")
            if str(mx).isdigit():
                n = int(mx)
                out.append(Journey(name=f"{r.path}: '{f.label}' keeps at most {n} characters", kind="validation", source="code", code_ref=f"{f.file}:{f.line}",
                                   steps=[Step("open", r.path), Step("type", r.path, f.label, f"{{long:{n + 10}}}", expect=f"maxlen:{n}", why=f"maxLength={n} in the code")]))
            if str(f.attrs.get("required", "")).lower() in ("true", "{true}") or (f.label or "").rstrip().endswith("*"):
                out.append(Journey(name=f"{r.path}: empty '{f.label}' is refused", kind="validation", source="code", code_ref=f"{f.file}:{f.line}",
                                   steps=[Step("open", r.path), *[Step("type", r.path, o.label, _value_for(o)) for o in fields[:5] if o is not f],
                                          Step("type", r.path, f.label, "{empty}"), Step("submit", r.path, submit.label, expect="rejected", why="the field is required in the code")]))
    return out


def journeys_from_ai(plan: dict | None) -> list[Journey]:
    out = []
    for j in (plan or {}).get("journeys", [])[:12]:
        if not isinstance(j, dict):
            continue
        steps = []
        for s in j.get("steps", [])[:25]:
            if isinstance(s, dict) and s.get("action") in ACTIONS:
                steps.append(Step(action=s["action"], screen=str(s.get("screen") or ""), target=str(s.get("target") or "")[:120], value=str(s.get("value") or "")[:300],
                                  expect=str(s.get("expect") or "")[:120], effect=str(s.get("effect") or ""), why=str(s.get("why") or "")[:200], oracle="ai"))
        if steps:
            out.append(Journey(name=str(j.get("name") or "AI journey")[:120], kind=str(j.get("kind") or "flow")[:20], source="ai", steps=steps, rationale=str(j.get("rationale") or "")[:300]))
    return out


# -- safety validation -----------------------------------------------------------------------------------
def validate(journeys: list[Journey], inv: CodeInventory, label_class: dict[str, str], repo: Path | None, observed_labels: set[str],
             secrets: tuple[str, ...] = (), allow_remote: bool = False) -> list[Journey]:
    """Authoritative: trims or blocks every journey the safety rules do not allow. Returns the journeys, each possibly with .blocked set."""
    bg = background_sync(repo)
    routes = {r.path for r in inv.routes}
    by_label: dict[tuple[str, str], CodeElement] = {}
    for e in inv.elements:
        if e.label:
            by_label.setdefault((e.route or "", normalize(e.label)), e)
            by_label.setdefault(("", normalize(e.label)), e)
    known = {normalize(e.label) for e in inv.elements if e.label} | observed_labels
    secrets = tuple(s for s in secrets if s and len(s) >= 3)
    for j in journeys:
        created = False
        kept: list[Step] = []
        for s in j.steps:
            if s.action not in ACTIONS:
                j.blocked = f"unknown action '{s.action}'"
                break
            if s.screen and s.screen not in routes and not s.screen.startswith("screen:"):
                j.blocked = f"screen '{s.screen}' is not in the code"
                break
            if any(sec in (s.value or "") for sec in secrets):
                s.value = "{qa}"  # never type the test account's credentials outside the sign-in form
            if s.action in ("type", "tap", "submit") and normalize(s.target) not in known:
                j.blocked = f"'{s.target}' is not a control in the code or on the device"
                break
            if s.action in ("tap", "submit"):
                el = by_label.get((s.screen, normalize(s.target))) if s.screen else by_label.get(("", normalize(s.target)))
                cls = label_class.get(normalize(s.target)) or classify_action(s.target)
                if cls in ("session_ending", "external", "system", "download"):
                    j.blocked = f"'{s.target}' is {cls.replace('_', '-')}: never activated"
                    break
                if BULK_RE.search(s.target):
                    j.blocked = f"'{s.target}' acts on records this run did not create (bulk action)"
                    break
                if cls == "destructive" and not created:
                    j.blocked = f"'{s.target}' deletes data and nothing before it creates this run's record"
                    break
                effect, why = code_effect(el, inv, repo, bg) if el is not None else screen_effect(s.screen, inv, repo, bg)
                s.effect = stricter(effect, s.effect or "none")  # the LLM may only raise caution
                s.why = why or s.why
                if s.effect == "remote" and not allow_remote:
                    j.blocked = f"'{s.target}' would send QA data to the backend ({why or 'per the plan'}); enable 'Let journeys create QA records on the backend' to allow it"
                    break
                if s.effect in ("local_record", "remote", "local_draft"):
                    created = True
            kept.append(s)
        if j.blocked and kept and any(s.action in ("type", "tap", "submit") for s in kept):
            j.steps = kept  # run what is allowed; the journey ends where the guard stops it
    return journeys


# -- running on the device -------------------------------------------------------------------------------
class JourneyRunner:
    def __init__(self, dev: AppDevice, explorer: AppExplorer, registry: QADataRegistry, inv: CodeInventory, events, label_class: dict[str, str]):
        self.dev = dev
        self.explorer = explorer
        self.registry = registry
        self.inv = inv
        self.events = events
        self.label_class = label_class
        self.results: list[JourneyResult] = []
        self.fields_by_label = {normalize(e.label): e for e in inv.elements if e.kind == "input" and e.label}

    # navigation
    def _screen(self, route: str) -> Screen | None:
        return next((s for s in self.explorer.screens if s.route == route and s.kind == "screen"), None) or next((s for s in self.explorer.screens if s.route == route), None)

    def _goto(self, route: str) -> bool:
        target = self._screen(route)
        return target is not None and self.explorer.return_to(target)

    def _find(self, snap: Snapshot, label: str, roles: set[str] | None = None) -> ObservedElement | None:
        want = normalize(label)
        cands = [e for e in snap.elements if e.visible and (roles is None or e.role in roles)]
        for e in cands:
            if normalize(e.name) == want:
                return e
        for e in cands:
            n = normalize(e.name)
            if want and n and (want in n or (n in want and len(n) > 3)):
                return e
        if self.dev.platform == "android" and label:
            probe = ObservedElement(selector="~" + label, role="button", name=label, tag="", bbox=[0, 10 ** 6, 1, 1])
            self.dev.scroll_into_view(probe)
            snap2 = self.dev.snapshot(0.4)
            for e in snap2.elements:
                if e.visible and normalize(e.name) == want and (roles is None or e.role in roles):
                    return e
        return None

    def _field(self, snap: Snapshot, label: str) -> ObservedElement | None:
        el = self._find(snap, label, {"textbox", "searchbox"})
        if el is not None:
            return el
        # RN text fields often expose the placeholder or the value, not the label: fall back to the code's order of fields
        code = [e for e in self.inv.elements if e.kind == "input" and e.route and e.label]
        route_fields = [e for e in code if e.label and normalize(e.label) == normalize(label)]
        if not route_fields:
            return None
        same_route = [e for e in code if e.route == route_fields[0].route and not e.attrs.get("secure")]
        boxes = [e for e in snap.elements if e.visible and e.role in ("textbox", "searchbox")]
        idx = next((i for i, e in enumerate(same_route) if e is route_fields[0]), -1)
        return boxes[idx] if 0 <= idx < len(boxes) else None

    def _hardware(self, snap: Snapshot) -> str:
        texts = " ".join(snap.texts[:40])
        m = HARDWARE_RE.search(texts)
        return m.group(0) if m else ""

    def _alive(self, snap: Snapshot | None) -> str:
        if snap is not None and snap.system_dialog:
            return snap.system_dialog
        if self.dev.app_state() == 1:
            return "the app is no longer running"
        errs = self.dev.errors_since_last()
        return errs[0][:200] if errs else ""

    # one journey
    def run(self, j: Journey, style: int = 0) -> JourneyResult:
        res = JourneyResult(journey=j)
        t0 = time.time()
        form_owned = False  # this journey typed a QA value into the current form
        context_owned = False  # the current screen is this run's record
        try:
            first = j.steps[0] if j.steps else None
            start = first.screen if first else ""
            if start and not self._goto(start):
                res.status, res.reason = "blocked", f"could not reach {start}: it was not reached during exploration, or the way back failed"
                return res
            self.dev.errors_since_last()
            for i, s in enumerate(j.steps):
                snap = self.dev.snapshot(0.4)
                entry = {"action": s.action, "target": s.target, "value": "", "expect": s.expect, "result": ""}
                res.log.append(entry)
                if s.action == "open":
                    entry["result"] = "on " + (snap.title or s.screen)
                elif s.action == "type":
                    el = self._field(snap, s.target)
                    code = self.fields_by_label.get(normalize(s.target))
                    if el is None:
                        required = code is not None and str(code.attrs.get("required", "")).lower() in ("true", "{true}")
                        if not required and s.expect == "" and s.value in ("{qa}", "{qa_note}", "{number}"):
                            entry["result"] = "not on screen; optional field skipped"
                            res.done = i + 1
                            continue
                        entry["result"] = "field not found"
                        res.status, res.reason = "blocked", f"field '{s.target}' not found on screen"
                        return res
                    mx = int(code.attrs["maxLength"]) if code is not None and str(code.attrs.get("maxLength", "")).isdigit() else None
                    value = self.registry.resolve(s.value, None if s.expect.startswith("maxlen") else mx, style)
                    original = self.dev.read_value(el)
                    self.dev.type_into(el, value)
                    self.dev.hide_keyboard()
                    res.actions += 1
                    entry["value"] = value if len(value) <= 60 else value[:57] + "…"
                    if self.registry.is_ours(value):
                        form_owned = True
                        res.qa_ids.append(value) if value not in res.qa_ids else None
                        if self.registry.is_ours(original):  # the field already holds this run's data: the value to put back is the one seen first
                            prev = next((r for r in self.registry.records if r["kind"] == "field" and r.get("route") == s.screen and r.get("label") == s.target and r.get("original") is not None), None)
                            original = prev["original"] if prev else None
                        self.registry.add(value, f"field '{s.target}'", s.screen or snap.title, j.name, kind="field", original=original,
                                          route=s.screen, label=s.target, submit=next((x.target for x in j.steps[i + 1:] if x.action == "submit"), ""))
                    if s.expect.startswith("maxlen:"):
                        n = int(s.expect.split(":", 1)[1])
                        now = self.dev.read_value(self._field(self.dev.snapshot(0.3), s.target) or el)
                        ok = len(now) <= n
                        entry["result"] = f"field holds {len(now)} characters"
                        if not ok:
                            res.status, res.reason = "fail", f"'{s.target}' accepted {len(now)} characters; the code sets maxLength={n}"
                            res.findings.append(self._finding(j, s, "medium", f"'{s.target}' accepts more than {n} characters", f"At most {n} characters (maxLength in the code)", res.reason))
                            return res
                    else:
                        entry["result"] = "typed"
                elif s.action in ("tap", "submit"):
                    el = self._find(snap, s.target, CLICK_ROLES)
                    if el is None:
                        entry["result"] = "control not found"
                        res.status, res.reason = "blocked", f"'{s.target}' not found on screen" + (f" (screen shows: {snap.title})" if snap.title else "")
                        return res
                    if not el.enabled:
                        entry["result"] = "disabled"
                        res.status, res.reason = "blocked", f"'{s.target}' is disabled on screen" + (f"; needs hardware: {self._hardware(snap)}" if self._hardware(snap) else "")
                        return res
                    cls = self.explorer.classify(el)
                    owner = row_owner(snap, el, self.registry)
                    if cls == "destructive" and not (owner or context_owned):
                        entry["result"] = "held: not this run's data"
                        res.held.append({"screen": s.screen, "label": s.target, "class": cls, "reason": "deletes data this run cannot prove it created"})
                        res.status, res.reason = "blocked", f"'{s.target}' held: it would delete data this run cannot prove it created"
                        return res
                    if cls == "mutating" and not (form_owned or owner or context_owned) and s.effect not in ("navigation", "none"):
                        entry["result"] = "held: form holds no QA data"
                        res.held.append({"screen": s.screen, "label": s.target, "class": cls, "reason": "the form holds no value this run typed"})
                        res.status, res.reason = "blocked", f"'{s.target}' held: the form does not hold this run's QA data"
                        return res
                    if cls in ("session_ending", "external", "system", "download"):
                        entry["result"] = f"held: {cls}"
                        res.held.append({"screen": s.screen, "label": s.target, "class": cls, "reason": "never activated"})
                        res.status, res.reason = "blocked", f"'{s.target}' is {cls}: never activated"
                        return res
                    if s.action == "submit" and (hw := self._hardware(snap)) and not form_owned:
                        res.status, res.reason = "blocked", f"needs hardware: the screen says '{hw}'"
                        entry["result"] = "blocked: hardware"
                        return res
                    before = snap
                    if not self.dev.tap(el):
                        entry["result"] = "tap failed"
                        res.status, res.reason = "blocked", f"could not tap '{s.target}'"
                        return res
                    res.actions += 1
                    if owner:
                        context_owned = True
                    time.sleep(1.0)
                    after = self.dev.snapshot(0.6)
                    if (dead := self._alive(after)):
                        entry["result"] = f"crash / error: {dead[:120]}"
                        res.evidence.append({"type": "screenshot", "path": self.dev.screenshot(f"journey_crash_{s.target}"), "viewport": "device"})
                        res.status, res.reason = "fail", f"after '{s.target}': {dead[:200]}"
                        res.findings.append(self._finding(j, s, "high", f"App fails during journey '{j.name}' at '{s.target}'", "The journey continues without a crash or error", res.reason, oracle="code"))
                        return res
                    moved = similar(self.explorer._names_of(before), self.explorer._names_of(after)) < 0.7
                    new_texts = [t for t in after.texts if t not in before.texts]
                    errors = [t for t in new_texts if ERROR_TEXT_RE.search(t)]
                    entry["result"] = ("moved to " + (after.title or "another screen")) if moved else ("error shown: " + errors[0][:80] if errors else "same screen")
                    if moved:
                        form_owned = False
                        context_owned = context_owned and owned_screen(after, self.registry)
                    if s.effect in ("local_record", "remote", "local_draft") and moved:
                        for qa in res.qa_ids:
                            for rec in self.registry.records:
                                if rec["id"] == qa and rec["kind"] == "field":
                                    rec["submitted"] = True
                        if s.effect in ("local_record", "remote"):
                            for qa in res.qa_ids:
                                self.registry.add(qa, "record", s.screen, j.name, kind="record", via=s.target)
                    verdict = self._check(s, moved, errors, after)
                    if verdict == "retry_format" and style < 2 and j.kind == "create":
                        # our value broke a format rule (e.g. letters, digits and dashes only): retry with a stricter QA value
                        self.events("journey", f"'{j.name}': the app refused the QA value ({errors[0][:80] if errors else ''}); retrying with a letters-digits-dashes value", None)
                        return self.run(j, style + 1)
                    if verdict not in ("ok", "retry_format"):
                        res.status, res.reason = verdict.split("|", 1)
                        res.evidence.append({"type": "screenshot", "path": self.dev.screenshot(f"journey_{s.target}"), "viewport": "device"})
                        sev = "medium"
                        title = (f"Invalid input accepted on {s.screen}: '{s.target}' went through" if s.expect == "rejected" else
                                 f"Submitting '{s.target}' on {s.screen} does not reach {s.expect.split(':', 1)[-1]}" if s.expect.startswith("navigates:") else
                                 f"Journey '{j.name}' step '{s.target}' did not do what was expected")
                        res.findings.append(self._finding(j, s, sev, title, s.expect, res.reason, oracle=s.oracle))
                        return res
                elif s.action in ("verify_text", "verify_absent"):
                    want = self.registry.resolve(s.value or "{qa}", None, style)
                    if s.screen and not self._goto(s.screen):
                        res.status, res.reason = "blocked", f"could not reach {s.screen} to check for '{want}'"
                        return res
                    present = self.dev.find_text(want)
                    entry["value"], entry["result"] = want, "shown" if present else "not shown"
                    if present != (s.action == "verify_text"):
                        res.status = "fail" if s.oracle == "code" else "needs_review"
                        res.reason = f"'{want}' is {'not ' if not present else ''}shown on {s.screen or 'the screen'}"
                        res.evidence.append({"type": "screenshot", "path": self.dev.screenshot(f"journey_verify_{s.screen}"), "viewport": "device"})
                        res.findings.append(self._finding(j, s, "high" if s.action == "verify_text" else "medium",
                                                          f"QA record {'missing' if not present else 'still shown'} on {s.screen or 'screen'} in journey '{j.name}'", s.expect or s.action, res.reason, oracle=s.oracle))
                        return res
                elif s.action == "relaunch":
                    if not self.explorer.can_relaunch:
                        res.status, res.reason = "blocked", "relaunching is not safe in this run (no test account to sign back in with)"
                        entry["result"] = "skipped"
                        return res
                    ok = self.explorer.relaunch()
                    entry["result"] = "relaunched" if ok else "relaunch failed"
                    if not ok:
                        res.status, res.reason = "blocked", "could not get back into the app after relaunching"
                        return res
                elif s.action == "back":
                    self.dev.back()
                    entry["result"] = "back"
                res.done = i + 1
            res.status = "pass" if not j.blocked else "blocked"
            res.reason = j.blocked or ("all steps done" + (f"; QA data {', '.join(res.qa_ids)}" if res.qa_ids else ""))
            res.evidence.append({"type": "screenshot", "path": self.dev.screenshot(f"journey_end_{j.name}"), "viewport": "device"})
            return res
        except Exception as exc:
            logger.exception("journey %s failed", j.name)
            res.status, res.reason = "blocked", f"journey stopped: {type(exc).__name__}: {str(exc)[:200]}"
            return res
        finally:
            res.duration_ms = int((time.time() - t0) * 1000)

    def _check(self, s: Step, moved: bool, errors: list[str], after: Snapshot) -> str:
        if s.expect == "rejected":
            if moved and not errors:
                return f"{'fail' if s.oracle == 'code' else 'needs_review'}|the app accepted it and moved on ({after.title})"
            return "ok"
        if s.expect in ("accepted",) or s.expect.startswith("navigates:"):
            if errors and not moved:
                fmt = any(re.search(r"letters|characters|digits|dash|format|use \d", e, re.I) for e in errors)
                return "retry_format" if fmt else f"{'fail' if s.oracle == 'code' else 'needs_review'}|the app refused the QA values: {errors[0][:160]}"
            if s.expect.startswith("navigates:") and not moved:
                return f"{'fail' if s.oracle == 'code' else 'needs_review'}|stayed on the same screen; the code navigates to {s.expect.split(':', 1)[1]}"
        return "ok"

    def _finding(self, j: Journey, s: Step, severity: str, title: str, expected: str, observed: str, oracle: str | None = None) -> FindingRecord:
        oracle = oracle or s.oracle
        conf = 0.9 if oracle == "code" else 0.55
        return FindingRecord(fingerprint=fingerprint(s.screen, "functional", title, j.name), route=s.screen or "journey", category="functional", severity=severity, title=title,
                             description=f"Journey '{j.name}' ({'planned from the code' if j.source == 'code' else 'planned by the LLM, checked by the safety rules'}); expectation from {'the code' if oracle == 'code' else 'the LLM: review before acting on it'}.",
                             expected=expected, observed=observed, standard_ref="ISO/IEC 25010 functional correctness", viewport="device", confidence=conf, reproduced_n=1, reproduced_m=1,
                             status=gate_status(conf) if oracle == "code" else "needs_review", evidence=[], code_ref=j.code_ref,
                             steps=[f"{x.action} {x.target} {x.value}".strip() for x in j.steps])

    # all journeys
    def run_all(self, journeys: list[Journey], budget_s: float = 600) -> list[JourneyResult]:
        end = time.time() + budget_s
        for j in journeys:
            if time.time() > end:
                self.results.append(JourneyResult(journey=j, status="blocked", reason="time budget for journeys used up"))
                continue
            if j.blocked and not any(s.action in ("type", "tap", "submit") for s in j.steps):
                self.results.append(JourneyResult(journey=j, status="blocked", reason=j.blocked))
                continue
            self.events("journey", f"Journey: {j.name} ({j.source}, {len(j.steps)} steps)", None)
            r = self.run(j)
            for f in r.findings:
                f.evidence = f.evidence or list(r.evidence)
            self.results.append(r)
            self.events("journey", f"  → {r.status}: {r.reason[:200]}", None, level="info" if r.status in ("pass", "blocked") else "warn")
            if self.explorer.home is not None:
                self.explorer.return_to(self.explorer.home)
        return self.results

    # cleanup
    def cleanup(self) -> None:
        """Restore fields this run overwrote and delete its records where the app allows; the rest stay listed as leftovers."""
        for rec in self.registry.pending():
            try:
                if rec["kind"] == "field":
                    self._restore_field(rec)
                elif rec["kind"] == "record":
                    self._delete_record(rec)
            except Exception as exc:
                self.registry.set_status(rec, "leftover", f"cleanup failed: {type(exc).__name__}: {str(exc)[:120]}")

    def _restore_field(self, rec: dict) -> None:
        if not rec.get("submitted"):
            self.registry.set_status(rec, "not_retained", "typed but never submitted; nothing was stored")
            return
        if not rec.get("route") or not self._goto(rec["route"]):
            self.registry.set_status(rec, "leftover", "could not reopen the form to check it")
            return
        snap = self.dev.snapshot(0.4)
        el = self._field(snap, rec["label"])
        now = self.dev.read_value(el) if el is not None else ""
        if not self.registry.is_ours(now):
            self.registry.set_status(rec, "not_retained", "the form no longer shows the QA value")
            return
        if rec.get("original") is None:
            self.dev.type_into(el, "")
            self.registry.set_status(rec, "leftover", "the app keeps the QA value as a draft and there was no earlier value to put back")
            return
        self.dev.type_into(el, rec["original"])
        self.dev.hide_keyboard()
        submit = self._find(self.dev.snapshot(0.3), rec.get("submit") or "", CLICK_ROLES) if rec.get("submit") else None
        if submit is not None:
            self.dev.tap(submit)
            time.sleep(1.0)
        self._goto(rec["route"])
        el = self._field(self.dev.snapshot(0.4), rec["label"])
        still = self.registry.is_ours(self.dev.read_value(el)) if el is not None else False
        self.registry.set_status(rec, "leftover" if still else "cleaned", "QA value still in the form" if still else "earlier value put back")

    def _delete_record(self, rec: dict) -> None:
        """Open the record (a row showing our value), use its delete control, confirm only that dialog, check it is gone."""
        lists = [s for s in self.explorer.screens if s.kind == "screen" and s.route]
        for scr in lists:
            if not self.explorer.return_to(scr) or not self.dev.find_text(rec["id"]):
                continue
            snap = self.dev.snapshot(0.4)
            row = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and row_owner(snap, e, self.registry) == rec["id"] or self.registry.is_ours(e.name)), None)
            delete = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and classify_action(e.name) == "destructive" and row_owner(snap, e, self.registry)), None)
            if delete is None and row is not None and self.dev.tap(row):
                snap = self.dev.snapshot(0.8)
                if owned_screen(snap, self.registry):
                    delete = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and classify_action(e.name) == "destructive" and not BULK_RE.search(e.name or "")), None)
            if delete is None:
                self.registry.set_status(rec, "leftover", f"shown on {scr.route}, but the app offers no way to delete it")
                return
            self.dev.tap(delete)
            dialog = self.dev.snapshot(0.8)
            confirm = next((e for e in dialog.elements if e.visible and e.role == "button" and CONFIRM_RE.search(e.name or "") and not BULK_RE.search(e.name or "")), None)
            if confirm is not None and any(e.in_dialog for e in dialog.elements):
                self.dev.tap(confirm)
                time.sleep(1.0)
            self.explorer.return_to(scr)
            gone = not self.dev.find_text(rec["id"])
            self.registry.set_status(rec, "cleaned" if gone else "leftover", "deleted in the app" if gone else "still listed after delete")
            return
        self.registry.set_status(rec, "leftover", "not found on any explored screen (it may live only on the backend)")


def to_tests(results: list[JourneyResult], registry: QADataRegistry) -> list[TestRecord]:
    status_of = {r["id"]: r["status"] for r in registry.records}
    out = []
    for i, r in enumerate(results):
        j = r.journey
        steps = r.log or [{"action": s.action, "target": s.target, "value": s.value, "result": "not run"} for s in j.steps]
        expected = "; ".join(f"{s.target or s.action}: {s.expect}" for s in j.steps if s.expect) or "Every step completes without a crash or an error"
        out.append(TestRecord(test_key=f"journey|{i}|{j.name}", name=f"Journey: {j.name}", technique="journey" if j.kind != "validation" else "validation",
                              category="functional", route=next((s.screen for s in j.steps if s.screen), ""), steps=steps, expected=expected, observed=r.reason,
                              status=r.status, duration_ms=r.duration_ms, evidence=r.evidence, code_ref=j.code_ref, qa_data=", ".join(r.qa_ids),
                              cleanup=", ".join(f"{q}: {status_of.get(q, '-')}" for q in r.qa_ids)))
    return out


def summary(results: list[JourneyResult], planner: str) -> dict:
    items = [{"name": r.journey.name, "source": r.journey.source, "kind": r.journey.kind, "status": r.status, "done": r.done, "steps": len(r.journey.steps), "reason": r.reason}
             for r in results]
    full = [r for r in results if r.status == "pass" and r.journey.kind != "validation"]
    return {"planner": planner, "total": len([r for r in results if r.journey.kind != "validation"]), "completed": len(full), "validation_cases": len([r for r in results if r.journey.kind == "validation"]),
            "items": items, "actions": sum(r.actions for r in results)}


def plan_dump(journeys: list[Journey]) -> list[dict]:
    return [asdict(j) for j in journeys]
