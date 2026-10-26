"""Run Action lists against a Playwright page with uniform failure handling."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from qaplatform.config import Settings
from qaplatform.execute.actions import Action, ActionType, Classification, Target

logger = logging.getLogger(__name__)


class ActionError(Exception):
    def __init__(self, message: str, classification: Classification, action: Action | None = None):
        super().__init__(message)
        self.classification = classification
        self.action = action


@dataclass
class StepResult:
    action: dict
    ok: bool
    observed: str = ""
    error: str = ""
    classification: str = ""
    duration_ms: int = 0


@dataclass
class RunOutcome:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    failed_step: int | None = None
    error: str = ""
    classification: str = ""
    duration_ms: int = 0


class PlaywrightExecutor:
    def __init__(self, page: Page, settings: Settings, screenshots_dir: Path, secrets: dict | None = None):
        self.page = page
        self.settings = settings
        self.shots = screenshots_dir
        self.shots.mkdir(parents=True, exist_ok=True)
        self.secrets = secrets or {}

    # ------------------------------------------------------------------
    def locate(self, target: Target | None, timeout: int | None = None) -> Locator:
        if target is None:
            raise ActionError("action needs a target", Classification.PLAN_ERROR)
        loc: Locator | None = None
        if target.selector:
            loc = self.page.locator(target.selector).first
            try:
                if loc.count() == 0 and target.role:
                    loc = None
            except Exception:
                loc = None
        if loc is None and target.role:
            loc = self.page.get_by_role(target.role, name=target.name, exact=target.exact).first if target.name else self.page.get_by_role(target.role).first
        if loc is None:
            raise ActionError("empty target", Classification.PLAN_ERROR)
        return loc

    def _value(self, v: str | None) -> str:
        if v is None:
            return ""
        return v.replace("<email>", self.secrets.get("email", "")).replace("<password>", self.secrets.get("password", ""))

    # ------------------------------------------------------------------
    def run(self, actions: list[Action]) -> RunOutcome:
        out = RunOutcome(ok=True)
        t0 = time.time()
        for idx, action in enumerate(actions):
            s0 = time.time()
            try:
                observed = self.execute(action)
                out.steps.append(StepResult(action.to_dict(), True, observed=str(observed or "")[:300], duration_ms=int((time.time() - s0) * 1000)))
            except ActionError as exc:
                out.steps.append(StepResult(action.to_dict(), False, error=str(exc)[:400], classification=exc.classification.value, duration_ms=int((time.time() - s0) * 1000)))
                out.ok = False
                out.failed_step = idx
                out.error = str(exc)[:400]
                out.classification = exc.classification.value
                break
            except PWTimeout as exc:
                out.steps.append(StepResult(action.to_dict(), False, error=str(exc)[:400], classification=Classification.TIMEOUT.value, duration_ms=int((time.time() - s0) * 1000)))
                out.ok = False
                out.failed_step = idx
                out.error = f"timeout: {str(exc)[:300]}"
                out.classification = Classification.TIMEOUT.value
                break
            except Exception as exc:
                cls = Classification.ENVIRONMENT_ERROR if "Target closed" in str(exc) or "browser has been closed" in str(exc).lower() else Classification.UNKNOWN
                out.steps.append(StepResult(action.to_dict(), False, error=f"{type(exc).__name__}: {str(exc)[:300]}", classification=cls.value, duration_ms=int((time.time() - s0) * 1000)))
                out.ok = False
                out.failed_step = idx
                out.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                out.classification = cls.value
                break
        out.duration_ms = int((time.time() - t0) * 1000)
        return out

    def execute(self, a: Action):
        t = a.timeout_ms or self.settings.action_timeout_ms
        page = self.page
        T = ActionType
        if a.type == T.NAVIGATE:
            try:
                page.goto(self._value(a.value), wait_until="domcontentloaded", timeout=self.settings.nav_timeout_ms)
            except PWTimeout as exc:
                raise ActionError(f"navigation timeout to {a.value}", Classification.NAVIGATION_ERROR, a) from exc
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except PWTimeout:
                pass
            page.wait_for_timeout(self.settings.settle_ms)
            return page.url
        if a.type == T.CLICK:
            loc = self.locate(a.target, t)
            try:
                loc.click(timeout=t)
            except PWTimeout as exc:
                raise ActionError(f"element not clickable: {a.target}", Classification.LOCATOR_ERROR, a) from exc
            page.wait_for_timeout(a.extra.get("settle_ms", self.settings.settle_ms))
            return page.url
        if a.type == T.FILL:
            loc = self.locate(a.target, t)
            loc.fill(self._value(a.value), timeout=t)
            return "filled"
        if a.type == T.PRESS:
            if a.target:
                self.locate(a.target, t).press(a.value or "Enter", timeout=t)
            else:
                page.keyboard.press(a.value or "Enter")
            page.wait_for_timeout(self.settings.settle_ms)
            return page.url
        if a.type == T.SELECT:
            self.locate(a.target, t).select_option(self._value(a.value), timeout=t)
            return "selected"
        if a.type == T.CHECK:
            loc = self.locate(a.target, t)
            if (a.value or "true").lower() == "true":
                loc.check(timeout=t)
            else:
                loc.uncheck(timeout=t)
            return "checked"
        if a.type == T.SET_VIEWPORT:
            w, h = (a.extra.get("width", 1440), a.extra.get("height", 900))
            page.set_viewport_size({"width": int(w), "height": int(h)})
            page.wait_for_timeout(300)
            return f"{w}x{h}"
        if a.type == T.RELOAD:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(self.settings.settle_ms)
            return page.url
        if a.type == T.GO_BACK:
            page.go_back(wait_until="domcontentloaded")
            page.wait_for_timeout(self.settings.settle_ms)
            return page.url
        if a.type == T.WAIT:
            page.wait_for_timeout(int(float(a.value or 1) * 1000))
            return "waited"
        if a.type == T.WAIT_URL:
            page.wait_for_url(lambda u: (a.value or "") in u, timeout=t)
            return page.url
        if a.type == T.EXPECT_URL_CONTAINS:
            if (a.value or "") not in page.url:
                raise ActionError(f"URL '{page.url}' does not contain '{a.value}'", Classification.VALIDATION_FAILURE, a)
            return page.url
        if a.type == T.EXPECT_URL_NOT_CONTAINS:
            if (a.value or "") in page.url:
                raise ActionError(f"URL '{page.url}' should not contain '{a.value}'", Classification.VALIDATION_FAILURE, a)
            return page.url
        if a.type == T.EXPECT_VISIBLE:
            loc = self.locate(a.target, t)
            try:
                loc.wait_for(state="visible", timeout=t)
            except PWTimeout as exc:
                raise ActionError(f"not visible: {a.target}", Classification.VALIDATION_FAILURE, a) from exc
            return "visible"
        if a.type == T.EXPECT_NOT_VISIBLE:
            loc = self.locate(a.target, t)
            try:
                loc.wait_for(state="hidden", timeout=t)
            except PWTimeout as exc:
                raise ActionError(f"still visible: {a.target}", Classification.VALIDATION_FAILURE, a) from exc
            return "hidden"
        if a.type == T.EXPECT_TEXT:
            needle = self._value(a.value)
            hay = self.locate(a.target, t).inner_text(timeout=t) if a.target else page.inner_text("body", timeout=t)
            if needle not in hay:
                raise ActionError(f"text '{needle}' not found", Classification.VALIDATION_FAILURE, a)
            return "text present"
        if a.type == T.EXPECT_NO_TEXT:
            needle = self._value(a.value)
            hay = self.locate(a.target, t).inner_text(timeout=t) if a.target else page.inner_text("body", timeout=t)
            if needle in hay:
                raise ActionError(f"unexpected text '{needle}' present", Classification.VALIDATION_FAILURE, a)
            return "text absent"
        if a.type == T.EXPECT_VALUE:
            actual = self.locate(a.target, t).input_value(timeout=t)
            if actual != self._value(a.value):
                raise ActionError(f"value '{actual}' != '{a.value}'", Classification.VALIDATION_FAILURE, a)
            return actual
        if a.type == T.EXPECT_ENABLED:
            if not self.locate(a.target, t).is_enabled(timeout=t):
                raise ActionError("element disabled", Classification.VALIDATION_FAILURE, a)
            return "enabled"
        if a.type == T.EXPECT_DISABLED:
            if self.locate(a.target, t).is_enabled(timeout=t):
                raise ActionError("element enabled", Classification.VALIDATION_FAILURE, a)
            return "disabled"
        if a.type == T.EXPECT_STORAGE:
            key = a.extra.get("key", "")
            present = page.evaluate("(k) => localStorage.getItem(k) !== null", key)
            want = (a.value or "present") == "present"
            if present != want:
                raise ActionError(f"localStorage['{key}'] {'present' if present else 'absent'}, expected {'present' if want else 'absent'}", Classification.VALIDATION_FAILURE, a)
            return "storage ok"
        if a.type == T.EVAL:
            return page.evaluate(a.value or "() => null")
        if a.type == T.SCREENSHOT:
            path = self.shots / f"{a.value or 'shot'}_{int(time.time()*1000)}.png"
            page.screenshot(path=str(path))
            return str(path)
        raise ActionError(f"unknown action {a.type}", Classification.PLAN_ERROR, a)
