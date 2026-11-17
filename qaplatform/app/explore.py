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

import logging
import re
import time
from dataclasses import dataclass, field

from qaplatform.app.device import AppDevice, Snapshot
from qaplatform.explore.crawler import ObservedElement
from qaplatform.explore.safety import classify_action
from qaplatform.reconcile.matcher import normalize

logger = logging.getLogger(__name__)

# Beyond the web rules: actions that change data or leave the app on a device.
APP_MUTATING = re.compile(r"\b(update|updates|copy|sync|upload|capture|record|scan|measure|take (a )?photo|retake|cut|paste|undo|redo|automatically|interval|enable|disable|allow|accept|agree|call|pay|buy|order|book)\b", re.I)
APP_DESTRUCTIVE = re.compile(r"\b(restart|quit|close (all|window|tab)|uninstall|factory|format|shut ?down|sleep|force)\b", re.I)
APP_EXTERNAL = re.compile(r"\b(hide|show all|support|about|website|open in browser|share|rate (us|app)|feedback|help center|privacy policy|terms|e-?mail us|whatsapp|minimi[sz]e|full ?screen|zoom$|bring all)\b|https?://|^[\w-]+/[\w.-]+$", re.I)
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
    result: str = "skipped"  # changed | same | crash | anr | error | foreign | not_found | skipped | not_reached
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
                 code_menu_items: set[str] | None = None):
        self.dev = dev
        self.events = events
        self.max_screens = max_screens
        self.max_depth = max_depth
        self.max_taps = max_screens * 12
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
        end = time.time() + timeout
        snap = self.dev.snapshot(0.5)
        while time.time() < end and not any(e.role in CLICK_ROLES or e.role in ("textbox", "searchbox") for e in snap.elements if e.visible):
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
        self.home, _ = self.register(first, [], 0)
        if self.dev.platform == "macos":
            self.open_menus()
        self.crawl(self.home)

    def open_menus(self) -> None:
        """Each application menu (never the Apple menu) is a screen of menu items."""
        snap = self.dev.snapshot(0.3)
        bar = [e for e in snap.elements if e.tag == "MenuBarItem" and e.name and e.name != "Apple"]
        for item in bar:
            if not self.dev.tap(item):
                continue
            opened = self.dev.snapshot(1.0)
            opened.elements = [e for e in opened.elements if e.tag == "MenuItem" and e.visible and e.name]
            opened.texts = [item.name]
            opened.activity = f"menu:{item.name}"
            scr, _ = self.register(opened, [item], 1, kind="menu")
            scr.route = f"menu:{item.name}"
            self.taps.append(Tap(screen_sig=self.home.sig, element=item, result="changed", to_sig=scr.sig, action_class="safe", detail="menu opened"))
            self.dev.keys(["XCUIKeyboardKeyEscape"])
            time.sleep(0.3)
            # each safe item: open the menu, choose it, see what happened, dismiss whatever it opened
            for entry in list(opened.elements):
                if not entry.enabled or len(self.taps) >= self.max_taps:
                    continue
                tap = Tap(screen_sig=scr.sig, element=entry, action_class=self.classify(entry))
                if self.code_menu_items and normalize(entry.name) not in self.code_menu_items and tap.action_class == "safe":
                    tap.action_class, tap.detail = "system", "added by macOS, not declared in the app's menus"
                self.taps.append(tap)
                if tap.action_class != "safe" or not self.dev.tap(item):
                    continue
                time.sleep(0.4)
                if not self.dev.tap(entry):
                    tap.result = "not_found"
                    self.dev.keys(["XCUIKeyboardKeyEscape"])
                    continue
                self.observe_after(self.home, tap)
                if tap.result in ("crash", "anr", "error"):
                    self.events("explore", f"{tap.result.upper()} after choosing {item.name} > {entry.name}: {tap.detail}", level="warn")
                self.dev.keys(["XCUIKeyboardKeyEscape"])
                self.return_to(self.home)

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
        return snap is not None and any(DIRTY_RE.search(t) for t in snap.texts + [e.name for e in snap.elements if e.visible])

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
