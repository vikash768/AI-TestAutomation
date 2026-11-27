"""Explore a running app: sign in if needed, then walk its screens.

The walk is depth-first from the first screen. Every safe control is tapped
once; what happened (another screen, nothing, a crash, an ANR, fatal errors in
the device log, another app in front) is recorded as a Tap. Taps are the
smoke tests of a native run: execute turns them into tests and findings, and
every failure is reproduced before it is reported.

The walk never taps a control the safety rules hold back (mutating,
destructive, session-ending, external) and never confirms anything. On macOS
it stays inside the app: the system Apple menu is never opened.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace

from qaplatform.app.device import APPLE_MENU_LABELS, AppDevice, Snapshot
from qaplatform.explore.crawler import ObservedElement
from qaplatform.explore.safety import classify_action
from qaplatform.reconcile.matcher import normalize

logger = logging.getLogger(__name__)

# Beyond the web rules: actions that change data or leave the app on a device.
APP_MUTATING = re.compile(r"\b(update|updates|copy|sync|upload|capture|record|scan|measure|take (a )?photo|retake|cut|paste|undo|redo|automatically|interval|enable|disable|allow|accept|agree|call|pay|buy|order|book)\b", re.I)
APP_DESTRUCTIVE = re.compile(r"\b(restart|quit|close (all|window|tab)|uninstall|factory|format|shut ?down|sleep|force)\b", re.I)
APP_EXTERNAL = re.compile(r"\b(hide|show all|support|about|website|open in browser|share|rate (us|app)|feedback|help center|privacy policy|terms|e-?mail us|whatsapp|minimi[sz]e|full ?screen|zoom$|bring all)\b|https?://|^[\w-]+/[\w.-]+$", re.I)
MAC_MENU_MUTATING = re.compile(r"\b(duplicate|revert|export|print)\b|^(new|open (recent|file|document)|speak|start dictation)\b", re.I)
MAC_MENU_EXTERNAL = re.compile(r"^(services|settings|preferences)(?:\s|[.…]|$)", re.I)
MAC_APP_SETTINGS = re.compile(r"^(settings|preferences)\s*(?:…|\.\.\.)?$", re.I)
LOGIN_BUTTON_RE = re.compile(r"\b(log ?in|sign ?in|continue|submit|next|proceed)\b", re.I)
EMAIL_HINT_RE = re.compile(r"(e-?mail|user ?name|login|phone|mobile|account)", re.I)
PASSWORD_HINT_RE = re.compile(r"(password|passcode|\bpin\b)", re.I)
DIRTY_RE = re.compile(r"(unsaved changes|not saved|unsaved|modified|edited)", re.I)
# a lock / PIN screen's way to the password form, and optional setup offered after sign-in
PASSWORD_ROUTE_RE = re.compile(r"\b(sign|log) ?in with (user ?name|password|e-?mail)|\buse (your |a )?password\b|password instead|another account", re.I)
SKIP_SETUP_RE = re.compile(r"^(skip( for now)?|not now|maybe later|later|no,? thanks)\b", re.I)
KEYPAD_KEY_RE = re.compile(r"^(\d|delete|backspace|clear|erase|⌫|←)$", re.I)
CLOSE_RE = re.compile(r"\b(close|cancel|dismiss|back|done|go back|navigate up)\b|^[x×✕]$", re.I)
CLICK_ROLES = {"button", "link", "menuitem", "tab", "switch", "checkbox", "radio", "combobox"}
DESKTOP_ROLES = {*CLICK_ROLES, "treeitem", "option"}
DESKTOP_POPUPS = {"PopUpButton", "ComboBox", "MenuButton"}
DESKTOP_TOGGLES = {"checkbox", "switch"}
DESKTOP_PERMISSION = re.compile(r"\b(grant|authorize|authorise|open system settings|open system preferences)\b", re.I)
DESKTOP_BACK = re.compile(r"^(back|go back|navigate up|cancel|dismiss)$", re.I)
DESKTOP_CONFIRM = re.compile(r"^(ok|okay|yes|confirm|apply|open|save|allow|finish|done)(?:\s|[.…]|$)", re.I)


@dataclass
class Screen:
    sig: str
    snap: Snapshot
    path: list[ObservedElement] = field(default_factory=list)  # taps from the first screen
    depth: int = 0
    screenshot: str = ""
    route: str = ""
    load_ms: int = 0
    kind: str = "screen"  # screen | menu | dialog

    @property
    def names(self) -> set[str]:
        return {f"{e.role}:{normalize(e.name)}" for e in self.snap.elements if e.visible}


@dataclass
class Tap:
    screen_sig: str
    element: ObservedElement
    action_class: str = "safe"
    result: str = "skipped"  # changed | updated | same | crash | anr | error | foreign | not_found | skipped | not_reached | state_unverified
    to_sig: str = ""
    errors: list[str] = field(default_factory=list)
    screenshot: str = ""
    ms: int = 0
    reproduced_n: int = 0
    reproduced_m: int = 0
    detail: str = ""


def similar(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


class AppExplorer:
    def __init__(self, dev: AppDevice, events, max_screens: int = 25, max_depth: int = 3, label_class: dict[str, str] | None = None,
                 code_menu_items: set[str] | None = None, budget_seconds: float | None = None):
        self.dev = dev
        self.events = events
        self.max_screens = max_screens
        self.max_depth = max_depth
        self.max_taps = max_screens * 12
        self.budget_seconds = budget_seconds
        self._desktop: _DesktopTraversal | None = None
        self.label_class = label_class or {}
        self.skip_labels: list[str] = []  # code labels of 'Skip for now'-style controls, to scroll to when below the fold
        # macOS adds items of its own to every app's menus (Close All, Writing Tools, AutoFill...): when the
        # code's menus are known, only the items the app declares are ever chosen
        self.code_menu_items = code_menu_items or set()
        self.mutator_shapes: set[tuple[str, int, int]] = set()  # (tag, w, h) of controls seen to edit the document
        self.screens: list[Screen] = []
        self.taps: list[Tap] = []
        self.tapped: set[tuple[str, str]] = set()
        self.home: Screen | None = None
        self.warnings: list[str] = []
        # A relaunch can end the app's session. Without a test account to sign back in with,
        # the walk must never relaunch a signed-in app, or the device is left signed out.
        self.credentials: tuple[str, str] = ("", "")
        self.started_signed_out = False

    @property
    def can_relaunch(self) -> bool:
        # macOS: never quit the user's desktop app (unsaved work); recover with Escape and re-activation only
        if self.dev.platform == "macos":
            return False
        return self.started_signed_out or all(self.credentials)

    def settled(self, timeout: float = 12.0) -> Snapshot:
        """The first snapshot with something to tap: splash and loading screens have none."""
        end = time.monotonic() + timeout
        snap = self.dev.snapshot(0.5)
        if self.dev.platform == "macos":
            usable = _DesktopTraversal._has_window_content
        else:
            usable = lambda view: any(e.role in CLICK_ROLES or e.role in ("textbox", "searchbox") for e in view.elements if e.visible)
        while time.monotonic() < end and not usable(snap):
            time.sleep(0.8)
            snap = self.dev.snapshot(0)
        return snap

    def relaunch(self) -> bool:
        """Relaunch the app and get back to where the run started; False when that is not safe or fails."""
        if not self.can_relaunch:
            return False
        self.dev.relaunch()
        return self.recover_session(self.settled())

    def recover_session(self, snap: Snapshot) -> bool:
        """From whatever the app shows after a launch: a PIN lock goes to its password form, the test account signs in, optional setup is skipped."""
        if self.started_signed_out or not all(self.credentials):
            return True
        if self.home is not None and similar(self.home.names, {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}) >= 0.7:
            return True
        if self.login_fields(snap)[1] is None:
            switch = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and PASSWORD_ROUTE_RE.search(e.name or "")), None)
            if switch is None or not self.dev.tap(switch):
                return True  # not a sign-in screen we recognise: let the caller check where it is
            snap = self.settled(6.0)
        if self.login_fields(snap)[1] is not None:
            ok, _, snap = self.sign_in(snap, *self.credentials)
            if not ok:
                return False
            snap = self.settled(6.0)
        self.skip_setup(snap)
        return True

    def skip_setup(self, snap: Snapshot) -> Snapshot:
        """Optional setup offered after sign-in ("Set a PIN" -> Skip for now) is skipped, never completed; skipping stores nothing."""
        for _ in range(2):
            if self.home is not None and similar(self.home.names, {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}) >= 0.7:
                return snap
            skip = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and SKIP_SETUP_RE.search(e.name or "")), None)
            for label in self.skip_labels if skip is None and self.dev.platform == "android" else []:
                # below the fold: the code says what the control is called, so scroll to it by that name
                self.dev.scroll_into_view(ObservedElement(selector="~" + label, role="button", name=label, tag="", bbox=[0, 10 ** 6, 1, 1]))
                snap = self.dev.snapshot(0.5)
                skip = next((e for e in snap.elements if e.visible and e.role in CLICK_ROLES and SKIP_SETUP_RE.search(e.name or "")), None)
                if skip is not None:
                    break
            if skip is None or not self.dev.tap(skip):
                return snap
            snap = self.settled(6.0)
        return snap

    # -- classification ---------------------------------------------------
    def classify(self, el: ObservedElement) -> str:
        name = el.name or ""
        code_cls = self.label_class.get(normalize(name))
        cls = classify_action(name, {"type": el.type})
        if cls == "safe":
            if APP_DESTRUCTIVE.search(name) or (self.dev.platform == "macos" and re.search(r"\bclose\b", name, re.I)):
                cls = "destructive"  # on a desktop, closing a window can discard the user's work
            elif APP_MUTATING.search(name):
                cls = "mutating"
            elif self.dev.platform == "macos" and APP_EXTERNAL.search(name):
                cls = "external"
            elif self.dev.platform == "macos" and el.tag == "MenuItem":
                if MAC_MENU_MUTATING.search(name):
                    cls = "mutating"
                elif MAC_MENU_EXTERNAL.search(name) and not MAC_APP_SETTINGS.fullmatch(name):
                    cls = "external"
        order = ["safe", "download", "external", "system", "mutating", "session_ending", "destructive"]
        if code_cls and order.index(code_cls) > order.index(cls if cls in order else "safe"):
            cls = code_cls
        return cls

    def register(self, snap: Snapshot, path: list[ObservedElement], depth: int, kind: str = "screen") -> tuple[Screen, bool]:
        names = {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}
        for s in self.screens:
            if s.kind == kind and similar(s.names, names) >= 0.7 and (not snap.activity or s.snap.activity == snap.activity):
                return s, False
        scr = Screen(sig=snap.signature, snap=snap, path=list(path), depth=depth, kind=kind)
        scr.screenshot = self.dev.screenshot(f"{kind}_{snap.title}")
        self.screens.append(scr)
        self.events("explore", f"Screen {len(self.screens)}: {snap.title!r} ({len(snap.elements)} controls)")
        return scr, True

    # -- sign in ----------------------------------------------------------
    @staticmethod
    def login_fields(snap: Snapshot) -> tuple[ObservedElement | None, ObservedElement | None, ObservedElement | None]:
        boxes = [e for e in snap.elements if e.role in ("textbox", "searchbox") and e.visible]
        pwd = next((e for e in boxes if e.type == "password" or PASSWORD_HINT_RE.search(e.name or "")), None)
        if pwd is None:
            return None, None, None
        user = next((e for e in boxes if e is not pwd and EMAIL_HINT_RE.search(e.name or "")), None) or next((e for e in boxes if e is not pwd), None)
        button = next((e for e in snap.elements if e.role == "button" and LOGIN_BUTTON_RE.search(e.name or "")), None)
        return user, pwd, button

    def sign_in(self, snap: Snapshot, email: str, password: str) -> tuple[bool, str, Snapshot]:
        user, pwd, button = self.login_fields(snap)
        if pwd is None:
            return True, "no sign-in form on the first screen", snap
        if not (email and password):
            return False, "the app shows a sign-in form but the run has no test account", snap
        if user is not None:
            self.dev.type_into(user, email)
        self.dev.type_into(pwd, password)
        self.dev.hide_keyboard()
        after = self.dev.snapshot(0.3)
        _, _, button = self.login_fields(after) if button is None else (None, None, button)
        if button is None or not self.dev.tap(button):
            return False, "could not find the sign-in button", after
        before = set(after.texts)
        for _ in range(15):
            time.sleep(1)
            after = self.dev.snapshot(0)
            if self.login_fields(after)[1] is None:
                return True, "signed in", after
        said = [t for t in after.texts if t not in before and len(t) > 3]
        return False, "still on the sign-in form 15 s after submitting the test account" + (f"; the app says: {said[0][:200]}" if said else ""), after

    # -- walking ----------------------------------------------------------
    def run(self, first: Snapshot) -> None:
        if self.dev.platform == "macos":
            self._desktop = _DesktopTraversal(self)
            self._desktop.run(first)
        else:
            self.home, _ = self.register(first, [], 0)
            self.crawl(self.home)

    def coverage_details(self) -> dict:
        return self._desktop.coverage() if self._desktop else {}

    @staticmethod
    def _state(snap: Snapshot) -> tuple:
        visible = [e for e in snap.elements if e.visible]
        # counts and positions too: expanding a section often reveals controls without names
        return {f"{e.role}:{normalize(e.name)}:{e.checked}" for e in visible}, sorted(snap.texts), len(visible), sorted(tuple(e.bbox) for e in visible)

    def observe_after(self, before: Screen, tap: Tap, just_before: Snapshot | None = None) -> Snapshot | None:
        """changed = another screen; updated = same screen responded (validation, toggle, toast); same = nothing at all."""
        t0 = time.time()
        snap = None
        try:
            snap = self.dev.snapshot(1.0)
        except Exception as exc:
            tap.detail = f"UI tree unreadable after the tap: {type(exc).__name__}"
        tap.ms = int((time.time() - t0) * 1000)
        state = self.dev.app_state()
        tap.errors = self.dev.errors_since_last()
        if snap is not None and snap.system_dialog:
            tap.result = "anr" if "respond" in snap.system_dialog.lower() else "crash"
            tap.detail = snap.system_dialog
        elif state == 1 and not (snap is not None and snap.foreign):
            tap.result = "crash"
            tap.detail = f"the app is no longer running (state {state})"
        elif snap is not None and snap.foreign:
            tap.result = "foreign"
            tap.detail = f"opened {snap.package}"
        elif self.dev.platform == "macos" and state in (2, 3):
            tap.result = "foreign"
            tap.detail = "The tested app moved out of the foreground"
        elif tap.errors:
            tap.result = "error"
        elif snap is not None:
            ref = just_before or before.snap
            if similar(self._names_of(ref), self._names_of(snap)) < 0.7:
                tap.result = "changed"
            elif self._state(ref) != self._state(snap):
                tap.result = "updated"
            elif toast := self.dev.toast():
                tap.result, tap.detail = "updated", f"toast: {toast[:120]}"
            else:
                tap.result = "same"
        if tap.result in ("crash", "anr", "error", "foreign") or tap.result == "same" and tap.element.role in ("button", "link"):
            tap.screenshot = self.dev.screenshot(f"after_{tap.element.name}")
        return snap

    @staticmethod
    def _dirty(snap: Snapshot | None) -> bool:
        return snap is not None and any(DIRTY_RE.search(t) for t in [snap.activity, *snap.texts, *(e.name for e in snap.elements if e.visible)])

    @staticmethod
    def _names_of(snap: Snapshot) -> set[str]:
        return {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}

    def _at(self, screen: Screen) -> tuple[bool, Snapshot | None]:
        try:
            snap = self.dev.snapshot(0.4)
        except Exception:
            return False, None
        ok = not snap.foreign and not snap.system_dialog and similar(screen.names, {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}) >= 0.7
        return ok, snap

    def return_to(self, screen: Screen) -> bool:
        """Back once (never from the first screen, which would leave the app), else relaunch and replay the path."""
        if self._desktop:
            return self._desktop.return_to(screen)
        ok, snap = self._at(screen)
        if ok:
            return True
        if snap is None or snap.foreign or snap.system_dialog or self.dev.app_state() != 4:
            self.dev.activate()
            time.sleep(1.5)
            ok, snap = self._at(screen)
            if ok:
                return True
        at_home = self.home is not None and snap is not None and similar(self.home.names, {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}) >= 0.7
        if not at_home or self.dev.platform == "macos":
            self.dev.back()
            ok, snap = self._at(screen)
            if ok:
                return True
            at_home = self.home is not None and snap is not None and similar(self.home.names, {f"{e.role}:{normalize(e.name)}" for e in snap.elements if e.visible}) >= 0.7
        if at_home and screen.path and self._replay(screen):
            return True  # from the first screen, the same taps that reached it; no relaunch needed
        for _ in range(2):
            if not self.relaunch():
                return False
            if self._replay(screen):
                return True
        return False

    def _replay(self, screen: Screen) -> bool:
        for el in screen.path:
            if not self.dev.tap(el):
                return False
            time.sleep(1.0)
        return self._at(screen)[0]

    def crawl(self, screen: Screen) -> None:
        if screen.kind == "menu":
            return
        keypad = sum(1 for e in screen.snap.elements if re.fullmatch(r"\d", e.name or "")) >= 10
        # controls that dismiss the screen go last, so its other controls are reached first
        for el in sorted(screen.snap.elements, key=lambda e: bool(CLOSE_RE.search(e.name or ""))):
            if len(self.taps) >= self.max_taps:
                self.warnings.append(f"stopped after {self.max_taps} taps")
                return
            if el.role not in CLICK_ROLES or not el.visible or not el.enabled or el.tag in ("MenuBarItem", "MenuItem"):
                continue
            if keypad and KEYPAD_KEY_RE.match(el.name or ""):
                continue  # a PIN pad: its keys type a secret, and what they change (dots) has no text to compare
            key = (screen.sig, f"{el.role}:{normalize(el.name)}:{el.selector}")
            if key in self.tapped:
                continue
            self.tapped.add(key)
            tap = Tap(screen_sig=screen.sig, element=el, action_class=self.classify(el))
            if not el.name and self.dev.platform == "macos":
                tap.action_class, tap.detail = "skipped", "unnamed control"
            self.taps.append(tap)
            if tap.action_class == "safe" and el.role == "button" and self.login_fields(screen.snap)[2] is el:
                # submitting the sign-in form is the sign-in tests' job (at most one failed attempt on the account)
                tap.action_class, tap.detail = "auth", "sign-in submit; covered by the sign-in tests"
            if tap.action_class == "safe" and len(el.bbox) == 4 and (el.tag, el.bbox[2], el.bbox[3]) in self.mutator_shapes:
                tap.action_class, tap.detail = "mutating", "same kind of control as one that edited the document"
            if tap.action_class != "safe":
                continue
            just_before = self.dev.snapshot(0.2)
            self.dev.errors_since_last()
            if not self.dev.tap(el):
                tap.result = "not_found"
                continue
            after = self.observe_after(screen, tap, just_before)
            if tap.result == "same" and el.role in ("button", "link"):
                # nothing at all happened: give it one more tap with a longer wait before believing it
                probe = Tap(screen_sig=screen.sig, element=el)
                if self.dev.tap(el):
                    time.sleep(1.5)
                    after = self.observe_after(screen, probe, just_before)
                tap.reproduced_n, tap.reproduced_m = (2 if probe.result == "same" else 1), 2
                tap.result = "same" if probe.result == "same" else probe.result
            if after is not None and self.dev.platform == "macos" and self._dirty(after) and not self._dirty(just_before):
                # the click edited the open document: undo it and hold back controls of the same kind
                self.dev.undo()
                time.sleep(0.5)
                still = self._dirty(self.dev.snapshot(0.3))
                tap.action_class, tap.detail = "mutating", "edited the document; " + ("undo did not clear the unsaved state" if still else "undone")
                if still:
                    self.warnings.append(f"'{el.name}' edited the open document and undo did not clear it")
                if len(el.bbox) == 4:
                    self.mutator_shapes.add((el.tag, el.bbox[2], el.bbox[3]))
            if el.role in ("checkbox", "switch") and tap.result in ("same", "changed", "updated"):
                self.restore_toggle(el, tap)
                continue
            if tap.result in ("crash", "anr", "error"):
                self.events("explore", f"{tap.result.upper()} after tapping {el.name!r} on {screen.snap.title!r}: {tap.detail or (tap.errors[:1] or [''])[0][:160]}", level="warn")
            if tap.result == "changed" and after is not None:
                nxt, new = self.register(after, screen.path + [el], screen.depth + 1, kind="dialog" if any(e.in_dialog for e in after.elements) else "screen")
                tap.to_sig = nxt.sig
                if new and nxt.depth <= self.max_depth and len(self.screens) < self.max_screens:
                    self.crawl(nxt)
            if tap.result not in ("same", "updated") and not self.return_to(screen):
                self.warnings.append(f"could not return to {screen.snap.title!r}; {sum(1 for e in screen.snap.elements if e.role in CLICK_ROLES)} controls there may be untested")
                return

    def restore_toggle(self, el: ObservedElement, tap: Tap) -> None:
        """A checkbox or switch is tapped twice: it must flip, then flip back to where it was."""
        before = el.checked
        now = next((e.checked for e in self.dev.snapshot(0.3).elements if e.selector == el.selector), None)
        self.dev.tap(el)
        restored = next((e.checked for e in self.dev.snapshot(0.5).elements if e.selector == el.selector), None)
        if before is not None and now is not None:
            if now == before:
                tap.result, tap.detail = "same", "state did not change when tapped"
            elif restored != before:
                tap.detail = "could not put it back the way it was"
                self.warnings.append(f"'{el.name}' was left {'on' if restored else 'off'}; it was {'on' if before else 'off'} before the run")
            else:
                tap.detail = "toggled and restored"

    def reproduce(self, tap: Tap, times: int = 2) -> None:
        """Re-run a failing tap from a fresh launch; record n/m."""
        screen = next((s for s in self.screens if s.sig == tap.screen_sig), None)
        if screen is None:
            return
        n = 0
        for _ in range(times):
            if self.can_relaunch:
                self.relaunch()
                ok = all(self.dev.tap(el) and time.sleep(1.0) is None for el in screen.path)
            else:
                ok = self.return_to(screen)
            if not ok:
                continue
            self.dev.errors_since_last()
            probe = Tap(screen_sig=screen.sig, element=tap.element)
            if not self.dev.tap(tap.element):
                continue
            self.observe_after(screen, probe)
            if probe.result == tap.result:
                n += 1
        tap.reproduced_n, tap.reproduced_m = n + 1, times + 1
        self.return_to(self.home)


@dataclass(frozen=True)
class _DesktopEdge:
    destination: str
    element: ObservedElement
    scroll_y: int = 0


class _DesktopTraversal:
    """Walk reachable accessibility states within the selected Mac app."""

    def __init__(self, explorer: AppExplorer):
        self.explorer = explorer
        self.dev = explorer.dev
        self.deadline = time.monotonic() + explorer.budget_seconds if explorer.budget_seconds is not None else None
        self.states: dict[str, Screen] = {}
        self.walked: set[str] = set()
        self.edges: dict[str, list[_DesktopEdge]] = {}
        self.menus: dict[str, tuple[Screen, list[ObservedElement]]] = {}
        self.actions = 0
        self.limits: list[str] = []
        self.content_visible = False
        self.safety_stop = ""

    def _limit(self, reason: str) -> str:
        if reason not in self.limits:
            self.limits.append(reason)
        return reason

    def _action_limit(self) -> str:
        if self.safety_stop:
            return self.safety_stop
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return self._limit(f"Desktop exploration reached its {self.explorer.budget_seconds:g}-second time budget")
        if self.actions >= self.explorer.max_taps:
            return self._limit(f"Desktop exploration reached its {self.explorer.max_taps}-action budget")
        return ""

    @staticmethod
    def _window_key(snap: Snapshot) -> str:
        # Tab changes can retain every actionable element. Include visible headings and roles,
        # while ignoring pixels and the global menu bar so they don't manufacture new windows.
        controls = sorted((e.tag, e.role, e.path_hint if e.selector.startswith("bounds:") else e.selector,
                           normalize(e.name), e.enabled, str(e.checked), str(e.expanded))
                          for e in snap.elements if e.visible and e.tag not in ("MenuBarItem", "MenuItem"))
        value = repr((snap.activity, controls, [normalize(t) for t in snap.texts[:40]]))
        return hashlib.sha1(value.encode()).hexdigest()[:12]

    @staticmethod
    def _has_window_content(snap: Snapshot) -> bool:
        return bool(snap.texts) or any(e.visible and e.name and e.tag not in ("MenuBarItem", "MenuItem") and
                                      not re.fullmatch(r"(close|minimi[sz]e|zoom|full screen)", e.name, re.I) for e in snap.elements)

    def _register(self, snap: Snapshot, path: list[ObservedElement], depth: int, kind: str = "screen") -> tuple[Screen | None, str]:
        sig = self._window_key(snap)
        if kind == "menu":
            # Menu contents can be identical in two namespaces. Retain the full branch path.
            sig = hashlib.sha1(repr((snap.activity, [(e.selector, e.name) for e in snap.elements])).encode()).hexdigest()[:12]
        if sig in self.states:
            return self.states[sig], ""
        if depth > self.explorer.max_depth:
            return None, self._limit(f"Desktop exploration reached its depth budget of {self.explorer.max_depth}")
        if len(self.states) >= self.explorer.max_screens:
            return None, self._limit(f"Desktop exploration reached its {self.explorer.max_screens}-state budget")
        screen = Screen(sig=sig, snap=snap, path=list(path), depth=depth, kind=kind)
        screen.screenshot = self.dev.screenshot(f"{kind}_{snap.title}")
        if kind == "menu":
            screen.route = snap.activity
        elif self._has_window_content(snap):
            self.content_visible = True
        self.states[sig] = screen
        self.explorer.screens.append(screen)
        self.explorer.events("explore", f"Desktop state {len(self.states)}: {snap.title!r}")
        return screen, ""

    def _snapshot(self, wait: float = 0.25) -> Snapshot | None:
        try:
            snap = self.dev.snapshot(wait)
        except Exception:
            return None
        return snap if not snap.foreign and not snap.system_dialog and self.dev.app_state() == 4 else None

    @staticmethod
    def _match(elements: list[ObservedElement], reference: ObservedElement) -> ObservedElement | None:
        candidates = [e for e in elements if e.visible and e.enabled and e.tag == reference.tag and
                      e.selector == reference.selector and normalize(e.name) == normalize(reference.name)]
        if len(candidates) == 1:
            return candidates[0]
        same_path = [e for e in candidates if reference.path_hint and e.path_hint == reference.path_hint]
        return same_path[0] if len(same_path) == 1 else None

    def _invoke(self, control: ObservedElement, *, scroll_y: int = 0, hover: bool = False) -> bool:
        if self._action_limit() or self.dev.app_state() != 4:
            return False
        self.actions += 1
        if scroll_y:
            return self.dev.scroll_area(control, scroll_y)
        if hover:
            return self.dev.hover_menu(control)
        return self.dev.tap(control)

    def _decision(self, control: ObservedElement) -> tuple[str, str]:
        if not control.enabled:
            return "disabled", "disabled in this accessibility state"
        if not control.name:
            return "skipped", "control has no accessible name"
        if control.tag == "MenuBarItem":
            return ("system", "macOS Apple menu") if control.name.casefold() in APPLE_MENU_LABELS else ("safe", "")
        action = self.explorer.classify(control)
        if action != "safe":
            return action, "held by desktop action rules"
        if DESKTOP_PERMISSION.search(control.name):
            return "system", "opens or changes permissions outside the tested UI"
        if control.in_dialog and DESKTOP_CONFIRM.search(control.name):
            return "mutating", "confirmation or action on a dialog"
        if control.role in DESKTOP_TOGGLES and control.checked is None:
            return "mutating", "initial toggle state is unavailable for restoration"
        if len(control.bbox) == 4 and (control.tag, control.bbox[2], control.bbox[3]) in self.explorer.mutator_shapes:
            return "mutating", "same control shape as an observed document edit"
        if control.tag == "MenuItem" and self.explorer.code_menu_items and normalize(control.name) not in self.explorer.code_menu_items:
            return "system", "item is not declared in the app's menus"
        return "safe", ""

    def _tap_record(self, screen: Screen, control: ObservedElement) -> Tap:
        action, detail = self._decision(control)
        if action == "safe" and self.explorer.login_fields(screen.snap)[2] is control:
            action, detail = "auth", "sign-in submit is covered by the sign-in checks"
        tap = Tap(screen_sig=screen.sig, element=control, action_class=action, detail=detail)
        self.explorer.taps.append(tap)
        return tap

    def _route_step(self, origin: str, destination: str) -> _DesktopEdge | None:
        pending = deque([(origin, None)])
        visited = {origin}
        while pending:
            current, first = pending.popleft()
            for edge in self.edges.get(current, ()):
                step = first or edge
                if edge.destination == destination:
                    return step
                if edge.destination not in visited:
                    visited.add(edge.destination)
                    pending.append((edge.destination, step))
        return None

    def _link(self, origin: str, destination: str, element: ObservedElement, scroll_y: int = 0) -> None:
        self.edges.setdefault(origin, []).append(_DesktopEdge(destination, element, scroll_y))

    def _recover_window(self, screen: Screen) -> Snapshot | None:
        snap = self._snapshot()
        if snap is None and self.dev.app_state() in (2, 3) and not self._action_limit():
            self.dev.activate()
            snap = self._snapshot(0.65)
        visited = set()
        escaped = False
        while snap is not None:
            here = self._window_key(snap)
            if here == screen.sig:
                return snap
            if here in visited or self._action_limit() or self.explorer._dirty(snap):
                return None
            visited.add(here)
            step = self._route_step(here, screen.sig)
            if step:
                controls = snap.scroll_areas if step.scroll_y else snap.elements
                current = self._match(controls, step.element)
                if current and (step.scroll_y or self._decision(current)[0] == "safe") and self._invoke(current, scroll_y=step.scroll_y):
                    snap = self._snapshot(0.55)
                    continue

            # A pane switch can usually be reversed using its previous selected tab. Use
            # the live accessibility element, never the previous coordinates or a guessed hotkey.
            selected = [e for e in screen.snap.elements if e.role == "tab" and e.checked is True and e.visible and e.enabled]
            back = [e for e in snap.elements if e.visible and e.enabled and DESKTOP_BACK.fullmatch(e.name)
                    and (e.name.casefold() not in ("cancel", "dismiss") or e.in_dialog)]
            candidates = [match for el in selected if (match := self._match(snap.elements, el)) and not match.checked] + back
            safe = next((e for e in candidates if self._decision(e)[0] == "safe"), None)
            if safe and self._invoke(safe):
                snap = self._snapshot(0.55)
                if snap is not None and self._window_key(snap) != here:
                    self._link(here, self._window_key(snap), safe)
                    continue
            if not escaped and self.dev.app_state() == 4:
                escaped = True
                self.actions += 1
                self.dev.back()
                snap = self._snapshot(0.4)
                # Closing a transient overlay can recover a window without changing its already-visible controls.
                continue
            return None
        return None

    @staticmethod
    def _menu_items(snap: Snapshot, owner: ObservedElement) -> list[ObservedElement]:
        items = [e for e in snap.elements if e.tag == "MenuItem" and e.visible]
        if not owner.path_hint.startswith("/"):
            return items
        prefix = owner.path_hint + "/"
        return [e for e in items if e.path_hint.startswith(prefix) and not any(
            parent.startswith("MenuItem[") for parent in e.path_hint.removeprefix(prefix).split("/")[:-1]
        )]

    def _show_menu(self, window: Screen, chain: list[ObservedElement]) -> tuple[Snapshot, list[ObservedElement]] | None:
        snap = self._recover_window(window)
        if snap is None:
            return None
        items = snap.elements
        for reference in chain:
            control = self._match(items, reference)
            if control is None or self._decision(control)[0] != "safe":
                return None
            children = self._menu_items(snap, control)
            if not children:
                if not self._invoke(control, hover=control.tag == "MenuItem"):
                    return None
                snap = self._snapshot(0.4)
                if snap is None:
                    return None
                children = self._menu_items(snap, control)
            if not children:
                return None
            items = children
        return snap, items

    def return_to(self, screen: Screen) -> bool:
        if self.safety_stop:
            return False
        menu = self.menus.get(screen.sig)
        if menu:
            result = self._show_menu(*menu)
            return result is not None
        return self._recover_window(screen) is not None

    @staticmethod
    def _unreachable(tap: Tap, reason: str) -> None:
        tap.result, tap.detail = "not_reached", reason

    def _stop_for_restoration(self, reason: str) -> None:
        self.safety_stop = reason
        self.explorer.warnings.append(reason)

    def _unverified_toggle(self, tap: Tap, reason: str) -> None:
        tap.result, tap.detail = "state_unverified", reason
        self._stop_for_restoration(reason)

    def _restore_toggle(self, tap: Tap, after: Snapshot) -> None:
        live = self._match(after.elements, tap.element)
        if live is None or live.checked is None:
            self._unverified_toggle(tap, f"Could not read '{tap.element.name}' after interacting with it")
            return
        if live.checked == tap.element.checked:
            latest = self._snapshot(0.65)
            live = self._match(latest.elements, tap.element) if latest is not None else None
            if live is None or live.checked is None:
                self._unverified_toggle(tap, f"Could not read the settled state of '{tap.element.name}'")
                return
            if live.checked == tap.element.checked:
                tap.result, tap.detail = "same", "The control stayed " + ("on" if live.checked else "off") + " after it was clicked"
                tap.screenshot = self.dev.screenshot(f"unchanged_{tap.element.name}")
                return
        # Restoration is part of the interaction, even when the next action budget was reached.
        if self.dev.app_state() == 4 and self.dev.tap(live):
            self.actions += 1
            snap = self._snapshot(0.4)
            restored = self._match(snap.elements, tap.element) if snap is not None else None
            if restored is not None and restored.checked == tap.element.checked:
                original, changed = ("on", "off") if restored.checked else ("off", "on")
                tap.result, tap.detail = "updated", f"Changed from {original} to {changed} and restored to {original}"
                return
        self._unverified_toggle(tap, f"Could not restore '{tap.element.name}' to its initial setting")

    def _dismiss_popup(self) -> None:
        if self.dev.app_state() == 4:
            self.actions += 1
            try:
                self.dev.back()
            except Exception:
                self.explorer.warnings.append("The app's open menu could not be dismissed")

    def _observe_tap(self, before: Screen, tap: Tap, initial: Snapshot, path: list[ObservedElement]) -> Screen | None:
        after = self.explorer.observe_after(before, tap, initial)
        if tap.result in ("crash", "anr", "foreign", "error"):
            return None
        if after is None:
            if tap.element.role in DESKTOP_TOGGLES:
                self._unverified_toggle(tap, f"Could not read '{tap.element.name}' after interacting with it")
            else:
                self._unreachable(tap, "the app did not return a readable accessibility state after tapping")
            return None
        if self.dev.app_state() != 4:
            return None
        if self.explorer._dirty(after) and not self.explorer._dirty(initial):
            self.dev.undo()
            latest = self._snapshot(0.5)
            restored = latest is not None and not self.explorer._dirty(latest)
            tap.result, tap.action_class = "skipped", "mutating"
            tap.detail = "document edit undone" if restored else "document edit could not be restored"
            if not restored:
                self._stop_for_restoration(f"'{tap.element.name}' edited the open document and undo did not clear it")
            if len(tap.element.bbox) == 4:
                self.explorer.mutator_shapes.add((tap.element.tag, tap.element.bbox[2], tap.element.bbox[3]))
            return None
        if tap.element.role in DESKTOP_TOGGLES:
            self._restore_toggle(tap, after)
            return None

        control = path[-1]
        if before.kind != "menu" and control.tag in DESKTOP_POPUPS:
            owner = self._match(after.elements, control) or control
            if items := self._menu_items(after, owner):
                browse_only = control.role == "combobox"
                self._walk_menu(before, before, [control], tap, opened=(after, items), browse_only=browse_only)
                if browse_only:
                    self._dismiss_popup()
                return None
            if not self._has_window_content(after) or self._window_key(initial) == self._window_key(after):
                self._unreachable(tap, "the dropdown or menu did not expose readable choices")
                self._dismiss_popup()
                return None

        if self._window_key(initial) == self._window_key(after) and tap.result == "same":
            # Give asynchronous navigation a brief chance to settle, without pressing the control twice.
            latest = self._snapshot(0.85)
            if latest is not None:
                after = latest
        if self._window_key(initial) == self._window_key(after):
            return None
        kind = "dialog" if any(e.visible and e.in_dialog for e in after.elements) else "screen"
        destination, reason = self._register(after, path, before.depth + 1, kind)
        tap.result = "changed"
        if reason:
            tap.detail = reason
        elif destination:
            tap.to_sig = destination.sig
            if before.kind != "menu":
                self._link(before.sig, destination.sig, tap.element)
        return destination

    def _walk_window(self, screen: Screen) -> None:
        if screen.sig in self.walked:
            return
        self.walked.add(screen.sig)
        controls = [e for e in screen.snap.elements if e.visible and e.role in DESKTOP_ROLES and
                    e.tag not in ("MenuBarItem", "MenuItem")]
        for reference in sorted(controls, key=lambda e: bool(CLOSE_RE.search(e.name))):
            tap = self._tap_record(screen, reference)
            if tap.action_class != "safe":
                continue
            if reason := self._action_limit():
                self._unreachable(tap, reason)
                continue
            before = self._recover_window(screen)
            if before is None:
                self._unreachable(tap, "could not safely return to this window or section")
                continue
            control = self._match(before.elements, reference)
            if control is None or self._decision(control)[0] != "safe" or not self._invoke(control):
                self._unreachable(tap, self._action_limit() or "control is no longer reliably accessible")
                continue
            if destination := self._observe_tap(screen, tap, before, [*screen.path, control]):
                self._walk_window(destination)

        for reference in screen.snap.scroll_areas:
            for direction in (1, -1):
                if self._action_limit():
                    return
                before = self._recover_window(screen)
                area = self._match(before.scroll_areas, reference) if before else None
                if not area:
                    continue
                distance = max(100, min(area.bbox[3] * 3 // 4, 1200)) if len(area.bbox) == 4 else 360
                delta_y = direction * distance
                if not self._invoke(area, scroll_y=delta_y):
                    continue
                after = self._snapshot(0.5)
                if after is None or self._window_key(after) == screen.sig:
                    continue
                destination, _ = self._register(after, screen.path, screen.depth + 1, screen.kind)
                if destination is not None:
                    self._link(screen.sig, destination.sig, area, delta_y)
                    if reverse := self._match(after.scroll_areas, area):
                        self._link(destination.sig, screen.sig, reverse, -delta_y)
                    self._walk_window(destination)

    def _walk_menu(self, window: Screen, parent: Screen, chain: list[ObservedElement], tap: Tap, *,
                   opened: tuple[Snapshot, list[ObservedElement]] | None = None, browse_only: bool = False) -> None:
        show = opened or self._show_menu(window, chain)
        if show is None:
            self._unreachable(tap, self._action_limit() or "the menu did not expose selectable items")
            return
        current, items = show
        menu_label = " > ".join(e.name for e in chain)
        snap = replace(current, elements=items, texts=[chain[-1].name], activity=f"menu:{menu_label}")
        screen, reason = self._register(snap, [*window.path, *chain], parent.depth + 1, "menu")
        tap.result, tap.detail = "changed", reason or ("dropdown opened" if browse_only else "application menu opened")
        if screen is None:
            return
        tap.to_sig = screen.sig
        self.menus[screen.sig] = (window, chain)
        if screen.sig in self.walked:
            return
        self.walked.add(screen.sig)
        for reference in items:
            choice = self._tap_record(screen, reference)
            if browse_only and choice.action_class == "safe":
                choice.action_class, choice.detail = "mutating", "changing this selection may persist outside the tested view"
            if choice.action_class != "safe":
                continue
            if reason := self._action_limit():
                self._unreachable(choice, reason)
                continue
            if reference.haspopup == "menu":
                self._walk_menu(window, screen, [*chain, reference], choice)
                continue
            scope = self._show_menu(window, chain)
            control = self._match(scope[1], reference) if scope else None
            if control is None or self._decision(control)[0] != "safe" or not self._invoke(control):
                self._unreachable(choice, self._action_limit() or "could not safely select this menu item")
                continue
            if destination := self._observe_tap(screen, choice, window.snap, [*screen.path, control]):
                self._walk_window(destination)

    def run(self, first: Snapshot) -> None:
        self.explorer.home, _ = self._register(first, [], 0)
        home = self.explorer.home
        if home is None:
            return
        self._walk_window(home)

        # The app-global menu bar may be reachable even after a window can no longer be
        # restored. The live window is then the context for its menu destinations.
        current = self._recover_window(home) or self._snapshot()
        if current is not None:
            window = self.states.get(self._window_key(current)) or home
            for bar in (e for e in current.elements if e.tag == "MenuBarItem" and e.visible and
                        e.name and e.name.casefold() not in APPLE_MENU_LABELS):
                tap = self._tap_record(window, bar)
                if tap.action_class == "safe":
                    self._walk_menu(window, window, [bar], tap)
            if self.dev.app_state() == 4 and not self._action_limit():
                self.dev.back()  # dismiss an open application menu
            self._recover_window(home)
        else:
            self.explorer.warnings.append("The desktop app was not available in the foreground for menu exploration")
        if not self.content_visible:
            self.explorer.warnings.append("The app window exposed no testable content through macOS accessibility")
        self.explorer.warnings.extend(self.limits)

    def coverage(self) -> dict:
        held = [tap for tap in self.explorer.taps if tap.result == "skipped"]
        unreachable = [tap for tap in self.explorer.taps if tap.result in ("not_reached", "not_found")]
        by_role: dict[str, dict[str, int]] = {}
        for tap in self.explorer.taps:
            role = by_role.setdefault(tap.element.role, {"observed": 0, "exercised": 0, "held": 0, "unreachable": 0})
            role["observed"] += 1
            outcome = "held" if tap.result == "skipped" else "unreachable" if tap.result in ("not_reached", "not_found") else "exercised"
            role[outcome] += 1
        return {
            "screens": len(self.states),
            "states_walked": len(self.walked),
            "controls_observed": len(self.explorer.taps),
            "exercised": len(self.explorer.taps) - len(held) - len(unreachable),
            "held": len(held),
            "unreachable": len(unreachable),
            "control_types": by_role,
            "limits": list(self.limits),
            "warnings": list(self.explorer.warnings),
            "unreachable_items": [{"screen": self.states[tap.screen_sig].snap.title, "label": tap.element.name, "reason": tap.detail}
                                  for tap in unreachable],
            "complete_within_scope": self.content_visible and not (
                self.explorer.warnings or self.limits or unreachable or (self.states.keys() - self.walked)
            ),
        }
