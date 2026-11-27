"""Native app checks: what the explorer saw, plus the checks that drive the device.

Static, per screen (from the UI tree, deterministic):
  blank screen, data placeholders shown to the user (undefined / NaN / [object Object]),
  controls with no accessible name or a name that is code, touch targets below 24 dp,
  overlapping controls, controls cut off at the screen edge, unmasked password fields

From the walk (every safe tap, reproduced before it is reported):
  crash, ANR, fatal / JavaScript errors, the code's navigation target not reached

Active (they drive the device, then put it back):
  cold start time, sign-in negatives, rotation, background / resume, relaunch keeps the
  session, offline feedback, input bounds (typed, never submitted, original value restored)
"""
from __future__ import annotations

import logging
import re
import time

from qaplatform.app.device import AppDevice, Snapshot
from qaplatform.app.explore import PASSWORD_ROUTE_RE, AppExplorer, Screen, Tap, similar
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord, fingerprint
from qaplatform.judge.gate import confidence, gate_status
from qaplatform.reconcile.matcher import normalize

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"(^|\W)(undefined|null|NaN|\[object Object\]|Invalid Date|\{\{\s*\w+\s*\}\})(\W|$)")
CODE_NAME_RE = re.compile(r"(^[a-z]+(\.[a-z_]+){2,}$|light-dark\(|rgba?\(|#[0-9a-f]{6}\b|var\(--|^[a-z]+_[a-z_]+$|\$\{|function\s*\(|=>)", re.I)
OFFLINE_MSG_RE = re.compile(r"(offline|no (internet|connection|network)|network (error|unavailable)|connection (lost|failed|error)|check your (internet|connection)|try again|retry|unable to (connect|reach)|could not (connect|load))", re.I)
LOGIN_ERROR_RE = re.compile(r"(invalid|incorrect|wrong|failed|error|not (found|match|valid)|does ?n.t (exist|match)|required|enter (a|your)|try again|unauthori[sz]ed)", re.I)
ACCOUNT_LEAK_RE = re.compile(r"(no (account|user)|user (not found|does not exist)|account (not found|does not exist)|email (not|isn.t) registered)", re.I)
NAV_LABEL_RE = re.compile(r"\b(forgot|view|details?|see (all|more)|more|open|show|settings|history|profile|help|next|back|menu|account|terms|privacy|learn|read)\b", re.I)
ICON_ONLY_TAGS = {"ImageButton", "ImageView", "Image", "Button", "ViewGroup", "View"}
WINDOW_CONTROL_ROLES = {"button", "link", "menuitem", "tab", "switch", "checkbox", "radio", "textbox", "combobox"}
DESKTOP_CONTROL_ROLES = WINDOW_CONTROL_ROLES | {"searchbox", "slider", "disclosure"}
MENU_TAGS = {"MenuBarItem", "MenuItem"}


def _finding(route: str, category: str, severity: str, title: str, expected: str, observed: str, standard: str, evidence: list, *, conf: float = 0.9,
             n: int = 1, m: int = 1, key: str = "", component_key: str | None = None, test_key: str | None = None, review: bool = False, viewport: str = "device") -> FindingRecord:
    status = "needs_review" if review else gate_status(conf)
    return FindingRecord(fingerprint=fingerprint(route, category, title, key), route=route, category=category, severity=severity, title=title, expected=expected,
                         observed=observed, standard_ref=standard, viewport=viewport, confidence=conf if not review else min(conf, 0.6), reproduced_n=n, reproduced_m=m,
                         status=status, evidence=[e for e in evidence if e], component_key=component_key, test_key=test_key)


def _shot(path: str, label: str = "device") -> dict | None:
    return {"type": "screenshot", "path": path, "viewport": label} if path else None


def _overlap(a: list[int], b: list[int]) -> float:
    if len(a) != 4 or len(b) != 4:
        return 0.0
    x = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    y = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    smaller = min(a[2] * a[3], b[2] * b[3]) or 1
    return x * y / smaller


def _contains(a: list[int], b: list[int]) -> bool:
    return len(a) == 4 and len(b) == 4 and a[0] <= b[0] and a[1] <= b[1] and a[0] + a[2] >= b[0] + b[2] and a[1] + a[3] >= b[1] + b[3]


def _spacing_exempt(el, others, px_per_dp: float) -> bool:
    """WCAG 2.5.8 spacing exception: a 24 dp circle centred on the target touches no other target."""
    cx, cy = el.bbox[0] + el.bbox[2] / 2, el.bbox[1] + el.bbox[3] / 2
    radius = 12 * px_per_dp
    for o in others:
        if o is el or len(o.bbox) != 4 or _contains(o.bbox, el.bbox) or _contains(el.bbox, o.bbox):
            continue
        dx = max(o.bbox[0] - cx, 0, cx - (o.bbox[0] + o.bbox[2]))
        dy = max(o.bbox[1] - cy, 0, cy - (o.bbox[1] + o.bbox[3]))
        if (dx * dx + dy * dy) ** 0.5 < radius:
            return False
    return True


def _desktop_geometry(screen: Screen, controls: list) -> tuple[list[TestRecord], list[FindingRecord]]:
    """Report layout candidates in each control's own window. Scroll areas can intentionally clip."""
    framed = {}
    for control in controls:
        window = screen.snap.element_windows.get(control.path_hint)
        if (control.enabled and window and len(control.bbox) == 4 and min(control.bbox[2:]) > 0
                and control.path_hint not in screen.snap.scrollable_elements):
            parent, marker, offset = control.path_hint.rpartition("/Window[")
            window_path = parent + marker + offset.partition("]")[0]
            framed.setdefault((window_path, window, control.in_dialog), []).append(control)

    clipped, overlaps = [], []
    for (_, (left, top, width, height), _), group in framed.items():
        right, bottom = left + width, top + height
        contained = []
        for control in group:
            x, y, w, h = control.bbox
            if x < left - 2 or y < top - 2 or x + w > right + 2 or y + h > bottom + 2:
                # Ignore elements completely outside the window. AX trees can keep layout entries for hidden panes.
                if x + w > left and x < right and y + h > top and y < bottom:
                    clipped.append(control)
            else:
                contained.append(control)
        for index, a in enumerate(contained):
            for b in contained[index + 1:]:
                if _contains(a.bbox, b.bbox) or _contains(b.bbox, a.bbox):
                    continue
                if _overlap(a.bbox, b.bbox) > 0.3:
                    overlaps.append((a, b))

    tests, findings = [], []
    candidates = (
        ("clipping", "Controls fit inside the window", clipped,
         ", ".join(f"{item.name or item.tag!r} {item.bbox}" for item in clipped[:5]),
         {"clipped": [item.bbox for item in clipped[:20]]}),
        ("overlap", "Controls have distinct hit areas", overlaps,
         "; ".join(f"{a.name or a.tag!r} / {b.name or b.tag!r}" for a, b in overlaps[:5]),
         {"overlap": [[a.bbox, b.bbox] for a, b in overlaps[:20]]}),
    )
    count = sum(len(group) for group in framed.values())
    for kind, expected, matched, detail, layout in candidates:
        test_key = f"desktop-{kind}|{screen.route}|{screen.sig}"
        observed = (f"{len(matched)} possible {kind} cases. {detail}" if matched else
                    f"No visible {kind} in {count} measurable controls outside scroll areas" if count else
                    "No measurable controls with window bounds outside scroll areas")
        tests.append(TestRecord(test_key=test_key, name=f"{screen.route}: window {kind}", technique="desktop-layout",
                                category="ui-responsive", route=screen.route, expected=expected, observed=observed,
                                status="needs_review" if matched else "pass" if count else "blocked",
                                evidence=[{"type": "layout", **layout}] if matched else []))
        if matched:
            findings.append(_finding(
                screen.route, "ui-responsive", "low", f"Possible window {kind}: {len(matched)} case(s)", expected,
                observed, "ISO/IEC 25010 operability", [_shot(screen.screenshot, "window"), {"type": "layout", **layout}],
                key=screen.sig, test_key=test_key, review=True, viewport="window",
            ))
    return tests, findings


def screen_checks(screens: list[Screen], density: float, platform: str) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    px_per_dp = density / 160.0 if platform == "android" and density else 1.0
    desktop = platform == "macos"
    for s in screens:
        snap, route, ev = s.snap, s.route, [_shot(s.screenshot, "window" if desktop else "device")]
        menu = s.kind == "menu"
        if menu and not desktop:
            continue
        clickable = [e for e in snap.elements if e.visible and (e.tag in MENU_TAGS) == menu
                     and e.role in (DESKTOP_CONTROL_ROLES if desktop else WINDOW_CONTROL_ROLES)]
        # blank
        blank = (not clickable and (menu or not any(t.strip() for t in snap.texts))) if desktop else not snap.elements and not any(t.strip() for t in snap.texts)
        tests.append(TestRecord(test_key=f"health|{route}|{s.sig}", name=f"{route} renders content", technique="page_health", category="stability", route=route,
                                expected="Menu exposes controls" if menu else "Screen shows text or controls",
                                observed=("No visible controls or content exposed by accessibility" if desktop else "blank screen") if blank else
                                f"{len(clickable) if desktop else len(snap.elements)} controls, {len(snap.texts)} texts",
                                status=("blocked" if desktop else "fail") if blank else "pass"))
        if blank and not desktop:
            findings.append(_finding(route, "stability", "high", "Screen renders blank", "Every screen shows content or a loading / empty state", "No text and no controls in the UI tree", "ISO/IEC 25010 reliability", ev, key=s.sig))
        # placeholders leaking into the UI
        leaks = sorted({t for t in snap.texts if PLACEHOLDER_RE.search(t)})[:5]
        tests.append(TestRecord(test_key=f"placeholder|{route}|{s.sig}", name=f"{route} shows no unformatted values", technique="content", category="functional", route=route,
                                expected="No 'undefined', 'null', 'NaN', '[object Object]' or 'Invalid Date' shown", observed="; ".join(leaks) or "none", status="fail" if leaks else "pass"))
        if leaks:
            findings.append(_finding(route, "functional", "medium", f"Unformatted value shown to the user: {leaks[0][:60]}", "Data is formatted, or a fallback is shown when it is missing",
                                     f"Visible text: {' | '.join(leaks)}", "ISO/IEC 25010 functional correctness", ev, key="|".join(leaks)))
        # accessible names
        unnamed = [e for e in clickable if not (e.name or "").strip()]
        code_names = [e for e in clickable if e.name and CODE_NAME_RE.search(e.name)]
        names_status = "fail" if unnamed or code_names else "pass"
        if desktop and not clickable:
            names_status = "blocked"
        elif desktop and code_names and not unnamed:
            names_status = "needs_review"
        tests.append(TestRecord(test_key=f"a11y-name|{route}|{s.sig}", name=f"{route}: every control has an accessible name", technique="accessibility", category="accessibility", route=route,
                                expected="Each control exposes a human-readable name",
                                observed="No visible controls to inspect" if desktop and not clickable else f"{len(unnamed)} unnamed, {len(code_names)} with code as the name",
                                status=names_status))
        if unnamed:
            named_expected = "Every control has a name a screen reader can announce"
            if not desktop:
                named_expected += " (accessibilityLabel / content-desc)"
            findings.append(_finding(route, "accessibility", "medium", f"{len(unnamed)} control(s) without an accessible name", named_expected,
                                     "Unnamed: " + ", ".join(f"{e.tag}@{e.bbox}" for e in unnamed[:6]), "WCAG 2.2 SC 4.1.2 Name, Role, Value", ev + [{"type": "layout", "unnamed": [e.bbox for e in unnamed[:20]]}], key=str(len(unnamed))))
        for e in code_names[:5]:
            findings.append(_finding(route, "accessibility", "medium", f"Control's accessible name is code: {e.name[:60]}", "A human-readable name", f"{e.tag} announced as '{e.name[:120]}'",
                                     "WCAG 2.2 SC 4.1.2 Name, Role, Value", ev, key=e.name, review=desktop))
        if desktop:
            if not menu:
                layout_tests, layout_findings = _desktop_geometry(s, clickable)
                tests.extend(layout_tests)
                findings.extend(layout_findings)
                _check_passwords(clickable, route, ev, findings, desktop=True)
            continue
        # touch targets (WCAG 2.5.8: 24 x 24 CSS px; on Android, dp)
        small = [e for e in clickable if len(e.bbox) == 4 and e.bbox[2] and e.bbox[3] and min(e.bbox[2], e.bbox[3]) / px_per_dp < 24 and e.role != "textbox"
                 and not _spacing_exempt(e, clickable, px_per_dp)]
        tests.append(TestRecord(test_key=f"target|{route}|{s.sig}", name=f"{route}: touch targets at least 24 dp", technique="responsive", category="ui-responsive", route=route,
                                expected="Every control is at least 24 x 24 dp", observed=f"{len(small)} smaller", status="fail" if small else "pass"))
        if small:
            findings.append(_finding(route, "ui-responsive", "low", f"{len(small)} touch target(s) smaller than 24 dp", "Controls are at least 24 x 24 dp (48 dp recommended on Android)",
                                     ", ".join(f"'{e.name[:30]}' {round(e.bbox[2] / px_per_dp)}x{round(e.bbox[3] / px_per_dp)} dp" for e in small[:6]), "WCAG 2.2 SC 2.5.8 Target Size (Minimum)", ev, key=str(len(small))))
        # overlaps between controls that are not nested
        overlaps = []
        for i, a in enumerate(clickable):
            for b in clickable[i + 1:]:
                if _contains(a.bbox, b.bbox) or _contains(b.bbox, a.bbox):
                    continue
                if _overlap(a.bbox, b.bbox) > 0.3:
                    overlaps.append((a, b))
        if overlaps:
            findings.append(_finding(route, "ui-responsive", "low", f"{len(overlaps)} pair(s) of overlapping controls", "Controls do not cover each other",
                                     "; ".join(f"'{a.name[:25]}' / '{b.name[:25]}'" for a, b in overlaps[:4]), "ISO/IEC 25010 operability", ev, key=str(len(overlaps)), review=True))
        # cut off at the edge
        if snap.width:
            left = snap.origin[0]
            clipped = [e for e in clickable if len(e.bbox) == 4 and e.bbox[2] and (e.bbox[0] < left - 2 or e.bbox[0] + e.bbox[2] > left + snap.width + 2)]
            if clipped:
                findings.append(_finding(route, "ui-responsive", "low", f"{len(clipped)} control(s) extend past the screen edge", "Controls fit within the screen width",
                                         ", ".join(f"'{e.name[:30]}' {e.bbox}" for e in clipped[:5]), "ISO/IEC 25010 operability", ev, key=str(len(clipped))))
        _check_passwords(snap.elements, route, ev, findings)
    return tests, findings


def _check_passwords(elements: list, route: str, evidence: list, findings: list[FindingRecord], *, desktop: bool = False) -> None:
    for element in elements:
        if element.role == "textbox" and re.search(r"password|passcode|\bpin\b", element.name or "", re.I) and element.type != "password":
            findings.append(_finding(route, "security", "high", f"Password field '{element.name[:40]}' is not masked",
                                     "Password input is masked" if desktop else "Password input is masked (secureTextEntry)",
                                     "The field is exposed as ordinary text input" if desktop else "The field reports password=false",
                                     "WSTG-ATHN" if desktop else "OWASP MASVS-PLATFORM / WSTG-ATHN", evidence, key=element.selector))


def tap_results(explorer: AppExplorer, by_screen_el: dict[tuple[str, str], ComponentRecord]) -> tuple[list[TestRecord], list[FindingRecord]]:
    """One test per tapped control; failures become findings with their reproduction count."""
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    screens = {s.sig: s for s in explorer.screens}
    desktop = explorer.dev.platform == "macos"
    for tap in explorer.taps:
        screen = screens.get(tap.screen_sig)
        route = screen.route if screen else ""
        comp = by_screen_el.get((tap.screen_sig, tap.element.selector))
        ckey = comp.key if comp else None
        expected_nav = (comp.intent or {}).get("navigates_to") if comp else []
        toggle = desktop and tap.element.role in ("switch", "checkbox")
        name = tap.element.name or tap.element.selector
        tkey = f"tap|{route}|{tap.element.selector}"
        if desktop and screen is not None:
            tkey = f"tap|{screen.sig}|{tap.element.path_hint or tap.element.selector}"
        steps = [{"tap": p.name or p.selector} for p in (screen.path if screen else [])] + [{"tap": name}]
        if tap.result in ("skipped", "not_reached"):
            if comp is not None and comp.status == "untested":
                comp.status = "skipped"
                note = tap.detail if desktop and tap.detail else f"not tapped ({tap.action_class})"
                comp.notes = f"{comp.notes}; {note}".strip("; ")
            if desktop:
                reason = tap.detail or ("Could not reach the control's screen" if tap.result == "not_reached" else f"Held by exploration rules ({tap.action_class})")
                tests.append(TestRecord(test_key=tkey, name=f"Tap '{name[:60]}' on {route}", technique="smoke",
                                        category="functional", route=route, steps=steps, expected="Control can be exercised",
                                        observed=reason, status="blocked", component_key=ckey))
            continue
        dead = tap.result == "same" and tap.element.role in ("button", "link")
        transient = dead and comp is not None and bool({"toast", "sheet"} & set((comp.intent or {}).get("opens") or []))
        status = {"changed": "pass", "updated": "pass", "same": "fail" if dead else "pass", "foreign": "pass", "not_found": "blocked"}.get(tap.result, "fail")
        observed = {"changed": "another screen opened", "updated": "the screen responded in place" + (f" ({tap.detail})" if tap.detail else ""),
                    "same": "nothing changed on screen after two taps" if dead else "no visible change", "foreign": tap.detail,
                    "not_found": "control not found when tapping"}.get(tap.result, f"{tap.result}: {tap.detail}")
        exp = "App stays responsive and in the foreground; no crash, ANR or fatal error"
        if expected_nav:
            exp += f"; navigates to {', '.join(expected_nav)}"
            if tap.result == "same":
                status = "fail"
                observed = "no screen change"
        if desktop and tap.result == "foreign":
            status, observed = "blocked", tap.detail or "The target app moved out of the foreground"
        elif desktop and dead and not expected_nav:
            status, observed = "needs_review", "No visible response in the tested window"
        if toggle and tap.result in ("updated", "same", "state_unverified"):
            exp = "Clicking changes the on/off state and the original setting can be restored"
            status = {"updated": "pass", "same": "fail", "state_unverified": "blocked"}[tap.result]
            observed = tap.detail
        if transient:
            # per the code it shows a short message or a sheet; one that closed before the check is not a dead control
            status, observed = "blocked", f"no lasting change; per the code ({comp.code_file}:{comp.code_line}) it shows a {' / '.join((comp.intent or {}).get('opens'))}, not seen in time to verify"
            dead = False
        if tap.errors:
            observed += " | log: " + tap.errors[0][:200]
        n, m = (tap.reproduced_n, tap.reproduced_m) if tap.reproduced_m else (1, 1)
        if status == "fail" and m > 1 and n < m:
            status = "flaky"
        ev = [_shot(tap.screenshot, "window" if desktop else "device"), {"type": "console", "messages": tap.errors[:10]} if tap.errors else None]
        tests.append(TestRecord(test_key=tkey, name=f"Tap '{name[:60]}' on {route}", technique="state-change" if toggle else "smoke",
                                category="functional" if toggle or tap.result == "same" else "stability", route=route,
                                steps=steps, expected=exp, observed=observed, status=status, duration_ms=tap.ms, reproduced_n=n, reproduced_m=m, component_key=ckey,
                                evidence=[item for item in ev if item] if desktop else []))
        if toggle and status == "fail" and tap.result == "same":
            findings.append(_finding(route, "functional", "medium", f"'{name[:50]}' does not toggle", exp, observed,
                                     "ISO/IEC 25010 functional correctness", ev, conf=confidence(n, m), n=n, m=m,
                                     key=tkey, component_key=ckey, test_key=tkey, viewport="window"))
        if comp is not None:
            comp.status = status if comp.status in ("untested", "pass", "skipped") else comp.status
        conf = confidence(n, m)
        if tap.result in ("crash", "anr"):
            findings.append(_finding(route, "stability", "critical", f"App {'crashes' if tap.result == 'crash' else 'stops responding'} when '{name[:50]}' is tapped", exp, observed,
                                     "ISO/IEC 25010 reliability (fault tolerance)", ev, conf=conf, n=n, m=m, key=tap.element.selector, component_key=ckey, test_key=tkey))
        elif tap.result == "error":
            findings.append(_finding(route, "stability", "high", f"Fatal or JavaScript error logged when '{name[:50]}' is tapped", "No error in the device log", observed,
                                     "ISO/IEC 25010 reliability", ev, conf=conf, n=n, m=m, key=tap.element.selector, component_key=ckey, test_key=tkey))
        elif dead and not expected_nav:
            promises = bool(NAV_LABEL_RE.search(name))
            if promises or not desktop:
                finding_title = f"No visible response after tapping '{name[:50]}'" if desktop else f"Tapping '{name[:50]}' does nothing"
                detail = observed if desktop else "No screen change, no dialog, no message and no log output after two taps"
                findings.append(_finding(route, "functional", "medium" if promises else "low", finding_title,
                                         "A control responds when tapped: navigates, opens something, or gives feedback", detail,
                                         "Nielsen #1 visibility of system status; ISO/IEC 25010 functional completeness", ev, conf=0.9 if n == m else 0.5,
                                         n=n, m=m, key=tap.element.selector, component_key=ckey, test_key=tkey, review=desktop or not promises))
        elif expected_nav and tap.result == "same":
            findings.append(_finding(route, "functional", "medium", f"'{name[:50]}' does not navigate to {expected_nav[0]}", f"Per the code ({comp.code_file}:{comp.code_line}) the handler navigates to {expected_nav[0]}",
                                     "Tapping left the same screen in place", "ISO/IEC 25010 functional correctness", ev, key=tap.element.selector, component_key=ckey, test_key=tkey, review=True))
    return tests, findings


class ActiveChecks:
    """Checks that change device state, each restoring what it changed."""

    def __init__(self, dev: AppDevice, explorer: AppExplorer, events, home_route: str, *,
                 by_screen_el: dict[tuple[str, str], ComponentRecord] | None = None):
        self.dev = dev
        self.explorer = explorer
        self.events = events
        self.home_route = home_route
        self.by_screen_el = by_screen_el or {}
        self.tests: list[TestRecord] = []
        self.findings: list[FindingRecord] = []

    def _names(self, snap: Snapshot) -> set[str]:
        return {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}

    def _alive(self) -> tuple[bool, Snapshot | None, str]:
        snap = None
        try:
            snap = self.dev.snapshot(0.8)
        except Exception as exc:
            return False, None, f"UI tree unreadable: {type(exc).__name__}"
        if snap.system_dialog:
            return False, snap, snap.system_dialog
        if self.dev.app_state() == 1 and not snap.foreign:
            return False, snap, "app is no longer running"
        return True, snap, ""

    def run(self, modules: set[str]) -> None:
        # checks that relaunch the app run last: a relaunch can sign the user out
        plan = [("perf", self.performance), ("rotation", self.rotation), ("resume", self.background_resume), ("inputs", self.inputs),
                ("launch", self.cold_start), ("offline", self.offline), ("relaunch", self.relaunch_keeps_session)]
        relaunching = {"launch", "offline", "relaunch"}
        for name, fn in plan:
            if name not in modules or (self.dev.platform == "macos" and name in ("perf", "rotation", "resume", "offline")):
                continue
            if name in relaunching and not self.explorer.can_relaunch:
                reason = ("Desktop runs preserve the existing app session by avoiding restarts" if self.dev.platform == "macos"
                          else "skipped: it relaunches the app, and without a test account the session could not be restored afterwards")
                self.tests.append(TestRecord(test_key=f"module|{name}", name=f"{name} checks", technique=name, category="platform", route=self.home_route,
                                             expected="Module completes", observed=reason,
                                             status="blocked"))
                continue
            t0 = time.time()
            try:
                fn()
                self.events(name, f"{name} checks done in {time.time() - t0:.0f}s")
            except Exception as exc:
                logger.exception("app check %s failed", name)
                self.tests.append(TestRecord(test_key=f"module|{name}", name=f"{name} checks", technique=name, category="platform", route="", expected="Module completes",
                                             observed=f"{type(exc).__name__}: {str(exc)[:300]}", status="blocked"))
                self.events(name, f"{name} checks failed: {type(exc).__name__}: {str(exc)[:200]}", level="warn")
            finally:
                try:
                    self.dev.set_network(True)
                    self.dev.rotate(False)
                    self.explorer.return_to(self.explorer.home)
                except Exception:
                    pass

    def cold_start(self) -> None:
        if not self.dev.build.app_id:
            return
        times = []
        exact = self.dev.platform == "android"
        if exact:
            times = self._android_cold_start()
            exact = bool(times)
        for _ in range(0 if times else 2):
            self.dev.driver.terminate_app(self.dev.build.app_id)
            time.sleep(1)
            t0 = time.time()
            self.dev.driver.activate_app(self.dev.build.app_id)
            for _ in range(60):
                snap = self.dev.snapshot(0.25)
                if snap.elements or snap.texts:
                    break
            times.append(time.time() - t0)
        if not times:
            return
        self.explorer.relaunch()  # signs back in if the restart signed the user out
        best = min(times)
        status = "pass" if best < 5 else "fail"
        how = "ActivityManager TotalTime" if exact else "wall clock incl. automation overhead"
        self.tests.append(TestRecord(test_key="launch|cold", name="Cold start time", technique="performance", category="performance", route=self.home_route,
                                     expected="First screen within 5 s of launch", observed=f"{best:.2f} s, {how} (runs: {', '.join(f'{t:.2f}' for t in times)})", status=status, duration_ms=int(best * 1000)))
        if status == "fail":
            self.findings.append(_finding(self.home_route, "performance", "low" if best < 8 else "medium", f"Cold start takes {best:.1f} s", "Cold start under 5 s (Android vitals: excessive at 5 s or more)",
                                          f"{best:.2f} s to first frame ({how})", "Android vitals: app startup time", [], key="cold", review=not exact))

    def _android_cold_start(self) -> list[float]:
        """ActivityManager's own measure (am start -W TotalTime): launch to first frame, no Appium overhead."""
        import subprocess

        from qaplatform.app.device import _adb

        pkg = self.dev.build.app_id
        activity = self.dev.build.activity or self.dev.driver.current_activity or ""
        if not activity:
            return []
        component = f"{pkg}/{activity}" if "/" not in activity else activity
        out = []
        for _ in range(3):
            subprocess.run([_adb(), "-s", self.dev.serial, "shell", "am", "force-stop", pkg], capture_output=True, timeout=30)
            time.sleep(1)
            res = subprocess.run([_adb(), "-s", self.dev.serial, "shell", "am", "start", "-W", "-n", component], capture_output=True, text=True, timeout=60)
            m = re.search(r"TotalTime:\s*(\d+)", res.stdout)
            if m:
                out.append(int(m.group(1)) / 1000)
        return out

    def performance(self) -> None:
        """Frame rendering and memory over the whole exploration, from Android's own counters (dumpsys)."""
        if self.dev.platform != "android" or not self.dev.build.app_id:
            return
        import subprocess

        from qaplatform.app.device import _adb

        pkg = self.dev.build.app_id
        gfx = subprocess.run([_adb(), "-s", self.dev.serial, "shell", "dumpsys", "gfxinfo", pkg], capture_output=True, text=True, timeout=60).stdout
        total = re.search(r"Total frames rendered:\s*(\d+)", gfx)
        janky = re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", gfx)
        p90 = re.search(r"90th percentile:\s*(\d+)ms", gfx)
        emulator = self.dev.serial.startswith("emulator-") or subprocess.run([_adb(), "-s", self.dev.serial, "shell", "getprop", "ro.kernel.qemu"], capture_output=True, text=True, timeout=20).stdout.strip() == "1"
        if total and janky and int(total.group(1)) >= 50:
            pct = float(janky.group(2))
            observed = f"{janky.group(1)} of {total.group(1)} frames janky ({pct:.1f}%), 90th percentile {p90.group(1) if p90 else '?'} ms" + (" on an emulator (software rendering: confirm on a real phone)" if emulator else "")
            status = "pass" if pct < 25 else "fail"
            self.tests.append(TestRecord(test_key="perf|frames", name="Smooth rendering during exploration", technique="performance", category="performance", route=self.home_route,
                                         expected="Fewer than 25% janky frames (Android vitals: slow rendering when over 50% of frames exceed 16 ms)", observed=observed, status=status))
            if status == "fail":
                self.findings.append(_finding(self.home_route, "performance", "medium" if pct >= 50 else "low", f"Slow or janky rendering: {pct:.0f}% of frames janky",
                                              "Under 25% janky frames", observed, "Android vitals: slow rendering", [], key="frames", review=pct < 50 or emulator))
        mem = subprocess.run([_adb(), "-s", self.dev.serial, "shell", "dumpsys", "meminfo", pkg], capture_output=True, text=True, timeout=60).stdout
        pss = re.search(r"TOTAL PSS:\s*(\d+)", mem) or re.search(r"^\s*TOTAL\s+(\d+)", mem, re.M)
        if pss:
            mb = int(pss.group(1)) / 1024
            status = "pass" if mb < 400 else "fail"
            self.tests.append(TestRecord(test_key="perf|memory", name="Memory use after exploring the app", technique="performance", category="performance", route=self.home_route,
                                         expected="Under 400 MB PSS", observed=f"{mb:.0f} MB PSS", status=status))
            if status == "fail":
                self.findings.append(_finding(self.home_route, "performance", "low", f"High memory use: {mb:.0f} MB", "Under 400 MB PSS", f"{mb:.0f} MB PSS after exploring the app",
                                              "ISO/IEC 25010 resource utilisation", [], key="memory", review=True))

    def rotation(self) -> None:
        if self.dev.platform != "android":
            return
        ok, before, _ = self._alive()
        if not ok or before is None:
            return
        if not self.dev.rotate(True):
            return
        time.sleep(1.5)
        alive, after, why = self._alive()
        shot = self.dev.screenshot("landscape")
        self.dev.rotate(False)
        time.sleep(1.5)
        _, back, _ = self._alive()
        kept = back is not None and similar(self._names(before), self._names(back)) >= 0.7
        status = "pass" if alive and kept else "fail"
        self.tests.append(TestRecord(test_key="state|rotation", name="Rotate to landscape and back", technique="state", category="stability", route=self.home_route,
                                     expected="App survives rotation and returns to the same screen", observed=why or ("same screen" if kept else "different screen after rotating back"), status=status))
        if not alive:
            self.findings.append(_finding(self.home_route, "stability", "critical", "App crashes on rotation", "Rotation keeps the app running", why, "ISO/IEC 25010 reliability", [_shot(shot, "landscape")], key="rot"))
        elif not kept:
            self.findings.append(_finding(self.home_route, "functional", "medium", "Screen state lost after rotation", "Same screen after rotating back", "A different screen is shown",
                                          "State-transition testing", [_shot(shot, "landscape")], key="rotstate", review=True))
        if after is not None and after.width:
            clipped = [e for e in after.elements if e.visible and len(e.bbox) == 4 and e.bbox[0] + e.bbox[2] > after.origin[0] + after.width + 2]
            if clipped:
                self.findings.append(_finding(self.home_route, "ui-responsive", "low", f"{len(clipped)} control(s) cut off in landscape", "Layout adapts to landscape",
                                              ", ".join(f"'{e.name[:30]}'" for e in clipped[:5]), "ISO/IEC 25010 operability", [_shot(shot, "landscape")], key="landclip", viewport="landscape"))

    def background_resume(self) -> None:
        ok, before, _ = self._alive()
        if not ok or before is None or self.dev.platform == "macos":
            return
        self.dev.background(3)
        time.sleep(1)
        alive, after, why = self._alive()
        kept = after is not None and similar(self._names(before), self._names(after)) >= 0.7
        self.tests.append(TestRecord(test_key="state|resume", name="Background for 3 s and resume", technique="state", category="stability", route=self.home_route,
                                     expected="Resumes on the same screen", observed=why or ("same screen" if kept else "different screen"), status="pass" if alive and kept else "fail"))
        if not alive:
            self.findings.append(_finding(self.home_route, "stability", "critical", "App crashes when resumed from the background", "App resumes", why, "ISO/IEC 25010 reliability", [], key="resume"))
        elif not kept:
            self.findings.append(_finding(self.home_route, "functional", "medium", "App does not resume where the user left it", "Same screen after resume", "A different screen is shown",
                                          "State-transition testing", [_shot(self.dev.screenshot("after_resume"))], key="resume-state", review=True))

    def relaunch_keeps_session(self) -> None:
        if self.explorer.login_fields(self.explorer.home.snap)[1] is not None:
            return  # started signed out; nothing to keep
        self.dev.relaunch()
        self.explorer.settled()  # past the splash screen
        alive, snap, why = self._alive()
        signed_out = snap is not None and self.explorer.login_fields(snap)[1] is not None
        locked = snap is not None and not signed_out and any(e.visible and PASSWORD_ROUTE_RE.search(e.name or "") for e in snap.elements)
        shot = self.dev.screenshot("relaunch") if signed_out or locked else ""
        if snap is not None and (signed_out or locked):
            self.explorer.recover_session(snap)
        observed = why or ("sign-in form shown" if signed_out else "PIN / biometric lock shown: the session is kept behind the device lock" if locked else "still signed in")
        self.tests.append(TestRecord(test_key="state|relaunch", name="Relaunch keeps the user signed in", technique="state", category="security", route=self.home_route,
                                     expected="After a relaunch the user is still signed in (a PIN / biometric lock counts)", observed=observed, status="pass" if alive and not signed_out else "fail"))
        if signed_out:
            self.findings.append(_finding(self.home_route, "functional", "medium", "Session is lost when the app is relaunched", "Session persists across relaunch",
                                          "Sign-in form after relaunch", "OWASP MASVS-AUTH", [_shot(shot)], key="relaunch", review=True))

    def offline(self) -> None:
        if self.dev.platform != "android":
            return
        nav, seen_to = [], set()
        for t in self.explorer.taps:
            if t.screen_sig == self.explorer.home.sig and t.result == "changed" and t.action_class == "safe" and t.to_sig and t.to_sig != self.explorer.home.sig and t.to_sig not in seen_to:
                seen_to.add(t.to_sig)
                nav.append(t)
        if not nav:
            nav = [None]  # still check the first screen itself offline
        if not self.dev.set_network(False):
            return
        try:
            time.sleep(2)
            self.explorer.relaunch()  # offline: only an offline sign-in (an account already used on this phone) can get back in
            for tap in nav[:4]:
                self.explorer.return_to(self.explorer.home)
                self.dev.errors_since_last()
                if tap is not None and not self.dev.tap(tap.element):
                    continue
                time.sleep(3)
                alive, snap, why = self._alive()
                errors = self.dev.errors_since_last()
                route = next((s.route for s in self.explorer.screens if tap is not None and s.sig == tap.to_sig), self.home_route)
                blank = snap is not None and not snap.elements and not snap.texts
                tkey = f"fault|offline|{route}"
                self.tests.append(TestRecord(test_key=tkey, name=f"Open {route} with no network", technique="fault", category="stability", route=route,
                                             expected="No crash or blank screen; offline state is explained", observed=why or ("blank" if blank else "ok") + (f" | log: {errors[0][:150]}" if errors else ""),
                                             status="fail" if (not alive or blank or errors) else "pass"))
                shot = _shot(self.dev.screenshot(f"offline_{route}"), "offline")
                if not alive:
                    self.findings.append(_finding(route, "stability", "critical", f"App crashes opening {route} offline", "Graceful offline handling", why, "ISO/IEC 25010 fault tolerance", [shot], key=route, test_key=tkey))
                elif blank:
                    self.findings.append(_finding(route, "stability", "high", f"{route} is blank when offline", "Cached data or an offline message", "Blank screen", "ISO/IEC 25010 fault tolerance", [shot], key=route, test_key=tkey))
                elif errors:
                    self.findings.append(_finding(route, "stability", "high", f"Unhandled error opening {route} offline", "Network failures are caught", errors[0][:300], "ISO/IEC 25010 fault tolerance",
                                                  [shot, {"type": "console", "messages": errors[:10]}], key=route, test_key=tkey))
        finally:
            self.dev.set_network(True)
            time.sleep(2)
            self.explorer.relaunch()

    def inputs(self) -> None:
        """Type boundary values into each reached text field, check it holds them, restore the original."""
        if self.dev.platform == "macos":
            self.desktop_inputs()
            return
        done = 0
        for screen in self.explorer.screens:
            boxes = [e for e in screen.snap.elements if e.role in ("textbox", "searchbox") and e.visible and e.enabled and e.type != "password"]
            if not boxes or done >= 8:
                continue
            if self.explorer.login_fields(screen.snap)[1] is not None:
                continue  # the sign-in form is covered by the sign-in tests
            if not self.explorer.return_to(screen):
                continue
            for box in boxes[:3]:
                tkey = f"input|{screen.route}|{box.selector}"
                component = self.by_screen_el.get((screen.sig, box.selector))
                component_key = component.key if component else None
                try:
                    original = self.dev.read_value(box)
                    self.dev.errors_since_last()
                    typed = self.dev.type_into(box, "A" * 300)
                    alive, _, why = self._alive()
                    errors = self.dev.errors_since_last()
                    kept = len(self.dev.read_value(box)) if typed else 0
                    self.dev.type_into(box, original if original and original != box.name else "")
                    self.dev.hide_keyboard()
                except Exception as exc:  # the field went away (a panel closed, the screen changed): this field is untested, the rest still run
                    self.tests.append(TestRecord(test_key=tkey, name=f"300 characters into '{box.name[:40]}'", technique="boundary", category="functional", route=screen.route,
                                                 expected="Field accepts or limits the input without crashing", observed=f"field not reachable: {type(exc).__name__}",
                                                 status="blocked", component_key=component_key))
                    continue
                done += 1
                self.tests.append(TestRecord(test_key=tkey, name=f"300 characters into '{box.name[:40]}'", technique="boundary", category="functional", route=screen.route,
                                             expected="Field accepts or limits the input without crashing", observed=why or f"field holds {kept} characters" + (f" | log: {errors[0][:120]}" if errors else ""),
                                             status="fail" if not alive or errors else "pass", component_key=component_key))
                if not alive:
                    self.findings.append(_finding(screen.route, "stability", "critical", f"App crashes on long input in '{box.name[:40]}'", "Long input is accepted or limited", why,
                                                  "ISTQB boundary value analysis", [], key=box.selector, test_key=tkey))
                    self.dev.activate()
                elif errors:
                    self.findings.append(_finding(screen.route, "stability", "high", f"Error logged on long input in '{box.name[:40]}'", "No error", errors[0][:300],
                                                  "ISTQB boundary value analysis", [{"type": "console", "messages": errors[:10]}], key=box.selector, test_key=tkey))

    def desktop_inputs(self) -> None:
        """Probe readable search fields and verify that their exact original values are restored."""
        attempted = 0
        restoration_failed = False
        for screen in self.explorer.screens:
            if screen.kind == "menu" or self.explorer.login_fields(screen.snap)[1] is not None:
                continue
            for box in screen.snap.elements:
                if not (box.role in ("textbox", "searchbox") and box.visible and box.enabled and box.type != "password"):
                    continue
                component = self.by_screen_el.get((screen.sig, box.selector))
                test = TestRecord(test_key=f"desktop-input|{screen.sig}|{box.path_hint or box.selector}",
                                  name=f"Desktop input '{box.name[:40] or box.tag}'", technique="boundary",
                                  category="functional", route=screen.route,
                                  expected="A search field handles a long query and its original value is restored", status="blocked",
                                  component_key=component.key if component else None)
                self.tests.append(test)
                if not (box.role == "searchbox" or normalize(box.name) in {"search", "find", "filter"}):
                    test.observed = "Held because this field may edit saved app data"
                    continue
                if restoration_failed or attempted >= 20:
                    test.observed = ("Further input checks stopped after a value could not be restored" if restoration_failed
                                     else "Desktop input check limit reached (20 probes)")
                    continue
                if not self.explorer.return_to(screen):
                    test.observed = "The field's screen could not be reached again"
                    continue
                try:
                    original = self.dev.read_value(box, strict=True)
                except Exception:
                    test.observed = "The original value could not be read reliably"
                    continue

                attempted += 1
                try:
                    typed = self.dev.type_into(box, "A" * 300)
                    alive, _, reason = self._alive()
                    if alive and typed:
                        kept = len(self.dev.read_value(box, strict=True))
                        test.status, test.observed = "pass", f"Search field accepted or limited the query to {kept} characters"
                    else:
                        test.observed = reason or "The query could not be entered"
                        if not alive and self.dev.app_state() == 1:
                            test.status = "fail"
                            self.findings.append(_finding(
                                screen.route, "stability", "critical", f"App stopped during input in '{box.name[:40]}'",
                                test.expected, test.observed, "ISO/IEC 25010 reliability", [_shot(screen.screenshot, "window")],
                                key=test.test_key, test_key=test.test_key, viewport="window",
                            ))
                except Exception as exc:
                    test.observed = f"Could not verify the search query ({type(exc).__name__})"
                finally:
                    try:
                        if self.dev.read_value(box, strict=True) != original:
                            self.dev.type_into(box, original)
                        restored = self.dev.read_value(box, strict=True) == original
                    except Exception:
                        restored = False
                    if restored:
                        test.cleanup = "Original value restored and verified"
                    else:
                        restoration_failed = True
                        if test.status != "fail":
                            test.status = "blocked"
                        test.cleanup = "Original value could not be verified; remaining input checks stopped"
                        self.events("inputs", f"Could not verify restoration of a search field on {screen.route}", level="warn")


def signin_negative(dev: AppDevice, explorer: AppExplorer, snap: Snapshot, email: str, route: str) -> tuple[list[TestRecord], list[FindingRecord]]:
    """Empty submit and one wrong password on the real account; nothing else."""
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    user, pwd, button = explorer.login_fields(snap)
    if pwd is None or button is None:
        return tests, findings
    original_user = dev.read_value(user) if user is not None else ""
    if original_user == (user.name if user is not None else None):
        original_user = ""  # Android reports the hint as the text of an empty field
    masked = pwd.type == "password"
    tests.append(TestRecord(test_key="auth|masked", name="Password field is masked", technique="auth", category="security", route=route, expected="Password input is masked",
                            observed="masked" if masked else "not masked", status="pass" if masked else "fail"))
    cases = [("empty", "", ""), ("wrong-password", email, "Wrong-Password-1!")] if email else [("empty", "", "")]
    for name, u, p in cases:
        if user is not None:
            dev.type_into(user, u)
        dev.type_into(pwd, p)
        dev.hide_keyboard()
        dev.errors_since_last()
        dev.tap(button)
        time.sleep(3)
        after = dev.snapshot(0.5)
        errors = dev.errors_since_last()
        still_form = explorer.login_fields(after)[1] is not None
        message = next((t for t in after.texts if LOGIN_ERROR_RE.search(t)), "")
        tkey = f"auth|{name}"
        ok = still_form and bool(message) and not errors
        tests.append(TestRecord(test_key=tkey, name=f"Sign-in with {name.replace('-', ' ')}", technique="auth", category="security", route=route,
                                expected="Stays on sign-in and says what is wrong", observed=("signed in!" if not still_form else message or "no message") + (f" | log: {errors[0][:120]}" if errors else ""),
                                status="pass" if ok else "fail"))
        shot = _shot(dev.screenshot(f"signin_{name}"))
        if not still_form:
            findings.append(_finding(route, "security", "critical", f"Sign-in accepted {name.replace('-', ' ')}", "Rejected", "The app left the sign-in screen", "OWASP WSTG-ATHN", [shot], key=name, test_key=tkey))
            break
        if not message:
            findings.append(_finding(route, "functional", "medium", f"No feedback after sign-in with {name.replace('-', ' ')}", "An error message explains the failure", "No message on screen",
                                     "Nielsen #9 help users recover from errors", [shot], key=name, test_key=tkey, review=True))
        if message and ACCOUNT_LEAK_RE.search(message):
            findings.append(_finding(route, "security", "medium", "Sign-in error reveals whether an account exists", "Generic message for any failed sign-in", message,
                                     "OWASP WSTG-IDNT-04 account enumeration", [shot], key="enum", test_key=tkey))
        if errors:
            findings.append(_finding(route, "stability", "high", f"Error logged on sign-in with {name.replace('-', ' ')}", "Failed sign-in is handled", errors[0][:300], "ISO/IEC 25010 reliability",
                                     [shot, {"type": "console", "messages": errors[:10]}], key=name, test_key=tkey))
    if user is not None:
        dev.type_into(user, original_user)  # put back a remembered user name
    dev.type_into(pwd, "")
    dev.hide_keyboard()
    if not masked:
        findings.append(_finding(route, "security", "high", "Password field is not masked", "secureTextEntry on the password input", "Typed characters are visible", "OWASP MASVS-PLATFORM", [], key="mask"))
    return tests, findings


def code_checks(inv) -> tuple[list[TestRecord], list[FindingRecord]]:
    """Bugs visible in the source alone, before anything runs."""
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    if not inv.routes:
        return tests, findings
    registered = {r.path for r in inv.routes}
    navigating = [e for e in inv.elements if e.navigates_to]
    broken = [(e, t) for e in navigating for t in e.navigates_to if t not in registered]
    tests.append(TestRecord(test_key="code|nav-targets", name="Every navigation target is a registered screen", technique="code", category="functional", route="codebase",
                            expected="navigate('X') only names screens the navigator declares", status="fail" if broken else "pass",
                            observed=f"{len(broken)} of {len(navigating)} navigating controls name an unregistered screen" if broken else f"all {len(navigating)} navigating controls name one of {len(registered)} registered screens"))
    for e, target in broken:
        f = _finding(e.route, "functional", "high", f"'{e.label or e.handler or e.tag}' navigates to a screen that is not registered: {target}", f"{target} is declared with <Stack.Screen name=\"{target}\">",
                     f"navigate('{target}') in {e.file}:{e.line}; registered screens: {', '.join(sorted(registered))}", "ISO/IEC 25010 functional correctness", [], key=f"nav-{target}-{e.file}:{e.line}",
                     test_key="code|nav-targets", viewport="code")
        f.code_ref = f"{e.file}:{e.line}"
        findings.append(f)
    return tests, findings
