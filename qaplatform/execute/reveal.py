"""Open what is closed at rest, then test what appears.

Menus, drawers, tabs, date pickers and confirm dialogs hold most of an admin
app's controls, and none of them is on screen when a page first loads. This
stage clicks every control the smoke stage saw open or change something, lists
the controls that appeared, matches them to the code's conditional components,
and click-tests the safe and mutating ones (writes are intercepted by the
guard) through the same oracle as the smoke stage. It goes two levels deep:
Actions menu > Edit Role > the drawer's form.

Every newly opened layer also gets three keyboard/assistive-technology checks
from the WAI-ARIA dialog pattern: exposed as a dialog, focus moves into it, and
Escape closes it.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from qaplatform.execute.smoke import CLOSE_RE, ComponentRecord, FindingRecord, SmokeRunner, TestRecord, fingerprint, step_of
from qaplatform.explore.crawler import ObservedElement, ObservedPage
from qaplatform.explore.safety import classify_action
from qaplatform.explore.scripts import LAYERS_JS
from qaplatform.ingest.js_parser import CodeElement
from qaplatform.reconcile.matcher import normalize, reconcile

ROLE_KIND = {"link": "link", "textbox": "input", "searchbox": "input", "spinbutton": "input", "combobox": "select", "listbox": "select", "checkbox": "checkbox",
             "radio": "radio", "switch": "switch", "tab": "tab", "menuitem": "menuitem", "menuitemcheckbox": "menuitem", "file": "file"}
MAX_OPENERS_PER_ROUTE = 25
MAX_REVEAL_CLICKS = 260
MAX_TESTS_PER_OPENER = 15
DATE_LIKE = re.compile(r"^(\d{1,2})( [a-z]{3,9})?( \d{4})?$|^[a-z]{3,9} \d{1,2},? \d{4}$|^\d{4} \d{2} \d{2}$")


def sig(e: dict) -> tuple:
    name = normalize(e.get("name"))
    if DATE_LIKE.fullmatch(name or ""):
        name = "#date"  # calendar cells and page numbers collapse to one signature
    return (e.get("role"), name)


def shape_key(comp: ComponentRecord) -> tuple:
    """The same control inside the same shared component, wherever it is opened from."""
    last = comp.reveal[-1] if comp.reveal else {}
    opener = last.get("file") or re.sub(r"\d+", "#", normalize(last.get("name")))
    return (opener, comp.role, sig({"role": comp.role, "name": comp.name or comp.label})[1])


def to_observed(e: dict) -> ObservedElement:
    return ObservedElement(**{k: v for k, v in e.items() if k in ObservedElement.__dataclass_fields__})


@dataclass
class LayerAudit:
    """Dialog checks aggregated by the layer's component, so one Drawer used on six pages is one finding."""
    findings: dict = field(default_factory=dict)
    tests: list = field(default_factory=list)

    def audit(self, runner: SmokeRunner, before: list[dict], after: list[dict], opener: str, route: str) -> None:
        seen = {l["signature"] for l in before}
        new = [l for l in after if l["signature"] not in seen and not l["isMenu"] and l["controls"] > 0]
        for layer in new[:1]:
            label = layer["text"][:40] or "dialog"
            checks = [
                ("role", layer["hasDialogRole"], "medium", f"Panel opened by '{opener}' is not exposed as a dialog",
                 "The opened panel has role=dialog (or alertdialog) so assistive technology announces it", "WCAG 2.2 4.1.2 Name, Role, Value"),
                ("focus", layer["focusInside"], "low", f"Focus stays behind the panel opened by '{opener}'",
                 "Keyboard focus moves into the panel when it opens", "WCAG 2.2 2.4.3 Focus Order; WAI-ARIA APG Dialog pattern"),
            ]
            escape_ok = None
            with contextlib.suppress(Exception):
                runner.page.keyboard.press("Escape")
                runner.page.wait_for_timeout(400)
                still = {l["signature"] for l in runner.page.evaluate(LAYERS_JS)}
                escape_ok = layer["signature"] not in still
            if escape_ok is not None:
                checks.append(("escape", escape_ok, "low", f"Panel opened by '{opener}' does not close with Escape",
                               "Escape closes the panel", "WAI-ARIA APG Dialog (Modal) pattern; WCAG 2.2 2.1.1 Keyboard"))
            for check, ok, sev, title, expected, ref in checks:
                self.tests.append(TestRecord(test_key=f"dialog-a11y|{check}|{route}|{opener}", name=f"Dialog check '{check}' for '{opener}' on {route}", technique="dialog-a11y",
                                             category="accessibility", route=route, expected=expected, observed=f"layer '{label}': {check} {'ok' if ok else 'missing'}",
                                             status="pass" if ok else "fail", reproduced_n=1, reproduced_m=1))
                if ok:
                    continue
                fp = fingerprint("dialog", check, layer["signature"])
                where = f"'{opener}' on {route}"
                if fp in self.findings:
                    f = self.findings[fp]
                    if where not in f.description:
                        f.description += f"; {where}"
                    continue
                shot = runner.screenshot(f"dialog_{check}_{route}_{opener}")
                self.findings[fp] = FindingRecord(fingerprint=fp, route=route, category="accessibility", severity=sev, title=title,
                                                  description=f"Layer `{layer['signature'][:80]}` ({label}). Seen after: {where}",
                                                  expected=expected, observed=f"{check} check failed", standard_ref=ref, confidence=0.9,
                                                  reproduced_n=1, reproduced_m=1, evidence=[x for x in [shot] if x])


class Revealer:
    def __init__(self, runner: SmokeRunner, events):
        self.r = runner
        self.events = events
        self.layers = LayerAudit()
        self.clicks = 0
        self.tested_shapes: dict[tuple, str] = {}

    # -- discovery -----------------------------------------------------------
    def openers(self, components: list[ComponentRecord]) -> list[ComponentRecord]:
        picked: dict[str, list[ComponentRecord]] = {}
        for c in components:
            if not self.r.clickable(c) or c.reveal or c.in_dialog or c.action_class != "safe":
                continue
            label = (c.label or c.name or "").strip()
            if CLOSE_RE.match(label):
                continue
            s = c.smoke_signals or {}
            opens = bool((c.intent or {}).get("opens"))
            changed_in_place = s.get("changed") and not s.get("url_changed") and not s.get("popups")
            popup_attr = (c.attrs or {}).get("haspopup") or c.role == "tab"
            if not (opens or s.get("opened") or changed_in_place or popup_attr):
                continue
            picked.setdefault(c.route, []).append(c)
        out: list[ComponentRecord] = []
        for route, items in picked.items():
            items.sort(key=lambda c: (0 if (c.intent or {}).get("opens") else 1 if (c.smoke_signals or {}).get("opened") else 2))
            out.extend(items[:MAX_OPENERS_PER_ROUTE])
        return out

    def open_and_list(self, opener: ComponentRecord) -> tuple[list[dict], str]:
        ok, why = self.r.prepare(opener.page_url, opener.reveal)
        if not ok:
            return [], why
        before = self.r.collect()
        layers_before = self.r.page.evaluate(LAYERS_JS)
        loc = self.r.locate(opener.selector, opener.role, opener.name or opener.label)
        if loc is None:
            return [], "opener not found"
        mark = self.r.guard.mark()
        try:
            loc.click(timeout=self.r.settings.action_timeout_ms)
        except Exception as exc:
            return [], f"opener not clickable: {type(exc).__name__}"
        self.r.page.wait_for_timeout(self.r.settings.settle_ms)
        self.clicks += 1
        if self.r.guard.since(mark):
            return [], "opener sends a write; not used to reveal"
        after = self.r.collect()
        layers_after = self.r.page.evaluate(LAYERS_JS)
        self.layers.audit(self.r, layers_before, layers_after, opener.label or opener.name, opener.route)
        self.r._dirty = True
        before_sigs = {sig(e) for e in before}
        return [e for e in after if e.get("visible") and sig(e) not in before_sigs], ""

    # -- matching ---------------------------------------------------------------
    def absorb(self, revealed: list[tuple[ComponentRecord | None, str, str, list, list[dict], str | None]], components: list[ComponentRecord],
               code_by_key: dict[str, CodeElement], protected_routes: list[str], origin: str) -> list[ComponentRecord]:
        """revealed: (opener, route, page_url, reveal_path, elements, opener_file). Returns the components to click next."""
        by_key = {c.key: c for c in components}
        unmatched_code = [code_by_key[c.key] for c in components if c.source == "code" and c.key in code_by_key]
        pages = []
        for opener, route, page_url, path, elements, opener_file in revealed:
            if not elements:
                continue
            pg = ObservedPage(route=route, url=page_url, protected=route in protected_routes or route == "(layout)" or bool(opener and opener.page_protected),
                              elements=[to_observed(e) for e in elements], revealed=True, opener_file=opener_file, reveal=path)
            pages.append(pg)
        if not pages:
            return []
        res = reconcile(unmatched_code, pages, protected_routes, origin)
        fresh: list[ComponentRecord] = []
        for m in res.matches:
            if m.observed is None or m.reason == "twin":
                continue
            comp = by_key.get(m.code.key)
            if comp is None or comp.source != "code":
                continue
            o = m.observed
            comp.source = "both"
            comp.selector, comp.role, comp.name = o.selector, o.role, o.name
            comp.href = o.href or comp.href
            comp.page_url, comp.page_protected = m.page.url, bool(m.page.protected)
            comp.visible, comp.enabled, comp.in_dialog = o.visible, o.enabled, False
            comp.reveal = list(m.page.reveal)
            if not comp.label or "{" in comp.label:
                comp.label = o.name
            comp.status = "observed"
            comp.notes = f"revealed by clicking {' > '.join(s.get('name') or '?' for s in comp.reveal)}; matched by {m.reason}"
            fresh.append(comp)
        seen: set = {(c.route, c.role, normalize(c.name), tuple((s.get("name") or "") for s in c.reveal)) for c in components if c.source == "observed"}
        for pg, o in res.unmatched_observed:
            key = (pg.route, o.role, sig({"role": o.role, "name": o.name})[1], tuple((s.get("name") or "") for s in pg.reveal))
            if key in seen:
                continue
            seen.add(key)
            intent = {"expected": ["Responds without errors"], "observed_only": True}
            cls = classify_action(o.name, {"href": o.href, "type": o.type, "download": True if o.download else None}, intent)
            comp = ComponentRecord(key=f"obs|{pg.route}|{o.role}|{o.name}|{'>'.join(s.get('name') or '' for s in pg.reveal)}"[:400], route=pg.route,
                                   kind=ROLE_KIND.get(o.role, "button"), label=o.name, source="observed", selector=o.selector, role=o.role, name=o.name, href=o.href,
                                   attrs={"type": o.type, "tag": o.tag}, intent={**intent, "action_class": cls}, action_class=cls, page_url=pg.url,
                                   page_protected=bool(pg.protected), visible=o.visible, enabled=o.enabled, in_dialog=False, reveal=list(pg.reveal), status="observed",
                                   notes="revealed by clicking " + " > ".join(s.get("name") or "?" for s in pg.reveal) + "; not declared in code")
            components.append(comp)
            fresh.append(comp)
        return fresh

    def dedupe(self, comps: list[ComponentRecord]) -> list[ComponentRecord]:
        """Test a shared component's controls once (a date picker on eight pages), cap tests per opener."""
        out: list[ComponentRecord] = []
        per_opener: dict[tuple, int] = {}
        for c in comps:
            key = shape_key(c)
            if key in self.tested_shapes:
                c.status = "equivalent"
                c.notes += f"; same control of the same shared component tested on {self.tested_shapes[key]}"
                continue
            opener = tuple((s.get("file") or s.get("name") or "") for s in c.reveal)
            if per_opener.get(opener, 0) >= MAX_TESTS_PER_OPENER:
                c.notes += "; not clicked (per-panel test cap)"
                continue
            per_opener[opener] = per_opener.get(opener, 0) + 1
            self.tested_shapes[key] = c.route
            out.append(c)
        return out

    # -- main ------------------------------------------------------------------------
    def run(self, components: list[ComponentRecord], code_by_key: dict[str, CodeElement], protected_routes: list[str], origin: str) -> tuple[list[TestRecord], list[FindingRecord], dict]:
        tests: list[TestRecord] = []
        findings: list[FindingRecord] = []
        openers = self.openers(components)
        stats = {"openers": len(openers), "revealed_level1": 0, "revealed_level2": 0, "matched_code": 0}
        with self.r.session():
            # level 1: open everything that opens something
            level1 = []
            for i, op in enumerate(openers, 1):
                if self.clicks >= MAX_REVEAL_CLICKS:
                    break
                if i % 5 == 0 or i == len(openers):
                    self.events("reveal", f"Opening {i}/{len(openers)}: '{op.label or op.name}' on {op.route}", 74 + int(4 * i / max(len(openers), 1)))
                elements, why = self.open_and_list(op)
                if why:
                    tests.append(TestRecord(test_key=f"reveal-open|{op.key}", name=f"Open '{op.label or op.name}' on {op.route}", technique="reveal", category="functional",
                                            route=op.route, expected="Reveals content", observed=why, status="blocked", component_key=op.key))
                    continue
                stats["revealed_level1"] += len(elements)
                level1.append((op, op.route, op.page_url, op.reveal + [step_of(op)], elements, op.code_file or None))
            fresh = self.absorb(level1, components, code_by_key, protected_routes, origin)
            stats["matched_code"] += sum(1 for c in fresh if c.source == "both")

            # click level-1 controls (and discover level 2 from what they open)
            level2 = []
            todo = self.dedupe([c for c in fresh if self.r.clickable(c)])
            for i, comp in enumerate(todo, 1):
                if self.clicks >= MAX_REVEAL_CLICKS:
                    comp.notes += "; not clicked (reveal budget reached)"
                    continue
                if i % 5 == 0 or i == len(todo):
                    self.events("reveal", f"Testing revealed control {i}/{len(todo)}: '{comp.label or comp.name}' on {comp.route}", 78 + int(3 * i / max(len(todo), 1)))
                t, f, res = self.r.test_component(comp, comp.page_protected, collect_after=True, technique="reveal-click")
                self.clicks += 1
                tests.append(t)
                if f:
                    findings.append(f)
                if res.get("after_elements") and res.get("before_elements") and not res["signals"].get("url_changed") and not CLOSE_RE.match((comp.label or "").strip()):
                    before = {sig(e) for e in res["before_elements"]}
                    new = [e for e in res["after_elements"] if e.get("visible") and sig(e) not in before]
                    if new:
                        stats["revealed_level2"] += len(new)
                        parent_file = comp.code_file or next((s.get("file") for s in comp.reveal if s.get("file")), None)
                        level2.append((comp, comp.route, comp.page_url, comp.reveal + [step_of(comp)], new, parent_file))
            fresh2 = self.absorb(level2, components, code_by_key, protected_routes, origin)
            stats["matched_code"] += sum(1 for c in fresh2 if c.source == "both")
            todo2 = self.dedupe([c for c in fresh2 if self.r.clickable(c)])
            for i, comp in enumerate(todo2, 1):
                if self.clicks >= MAX_REVEAL_CLICKS:
                    comp.notes += "; not clicked (reveal budget reached)"
                    continue
                if i % 5 == 0 or i == len(todo2):
                    self.events("reveal", f"Testing level-2 control {i}/{len(todo2)}: '{comp.label or comp.name}' on {comp.route}", 81)
                t, f, _ = self.r.test_component(comp, comp.page_protected, technique="reveal-click")
                self.clicks += 1
                tests.append(t)
                if f:
                    findings.append(f)
        tests.extend(self.layers.tests)
        findings.extend(self.layers.findings.values())
        stats["clicks"] = self.clicks
        return tests, findings, stats
