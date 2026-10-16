"""
Appium Action Executor.

Translates structured Action objects into Appium WebDriver commands for
native and hybrid application testing (desktop, Android, iOS).

This executor parallels SeleniumExecutor in interface but targets native
apps via Appium. Common actions (CLICK, TYPE, VERIFY_TEXT, etc.) are
supported; web-only actions are rejected with clear errors.

Actions are data-only (from schema.py). This executor handles HOW they
are performed on a native app.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from appium.webdriver.webdriver import WebDriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from automation.actions.schema import (
    Action,
    ActionType,
    FailureClassification,
    Locator,
    LocatorStrategy,
    WEB_ONLY_ACTIONS,
)
from automation.secrets.resolver import SecretResolutionError, SecretResolver
from automation.secrets.sanitizer import sanitize_filename_component, sanitize_text

logger = logging.getLogger(__name__)

# Re-use ActionExecutionError from the Selenium executor to keep a single
# exception type across the framework.
from automation.actions.executor import ActionExecutionError


class AppiumExecutor:
    """Executes structured Action objects via Appium WebDriver.

    This class takes a live Appium WebDriver instance and translates
    Action objects into deterministic native-app commands.
    """

    # Map LocatorStrategy to Appium By
    _BY_MAP = {
        LocatorStrategy.ID: (By.ID, lambda v: v),
        LocatorStrategy.NAME: (By.NAME, lambda v: v),
        LocatorStrategy.XPATH: (By.XPATH, lambda v: v),
        LocatorStrategy.CSS: (By.CSS_SELECTOR, lambda v: v),
        LocatorStrategy.DATA_TESTID: (By.CSS_SELECTOR, lambda v: f'[data-testid="{v}"]'),
        LocatorStrategy.TEXT: (
            By.XPATH,
            lambda v: (
                "//*[contains(@text, 'justpaid') or contains(@content-desc, 'justpaid') or contains(@text, 'ljustpaid') or contains(@text, 'Ijustpaid')]"
                if "justpaid" in v.lower()
                else f"//*[contains(@text,'{v}') or contains(@label,'{v}') or contains(@name,'{v}') or contains(@content-desc,'{v}') or contains(translate(@text,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{v.lower()}') or contains(translate(@content-desc,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{v.lower()}')]"
            )
        ),
        LocatorStrategy.LINK_TEXT: (By.LINK_TEXT, lambda v: v),
        LocatorStrategy.PARTIAL_LINK_TEXT: (By.PARTIAL_LINK_TEXT, lambda v: v),
        LocatorStrategy.ACCESSIBILITY_ID: (AppiumBy.ACCESSIBILITY_ID, lambda v: v),
        LocatorStrategy.CLASS_NAME: (By.CLASS_NAME, lambda v: v),
        LocatorStrategy.TAG_NAME: (By.TAG_NAME, lambda v: v),
        LocatorStrategy.ANDROID_UIAUTOMATOR: (AppiumBy.ANDROID_UIAUTOMATOR, lambda v: v),
        LocatorStrategy.IOS_PREDICATE: (AppiumBy.IOS_PREDICATE, lambda v: v),
    }

    # Key name mapping for PRESS_KEY action
    _KEY_MAP = {
        "ENTER": Keys.ENTER,
        "TAB": Keys.TAB,
        "ESCAPE": Keys.ESCAPE,
        "BACKSPACE": Keys.BACKSPACE,
        "DELETE": Keys.DELETE,
        "SPACE": Keys.SPACE,
        "ARROW_UP": Keys.ARROW_UP,
        "ARROW_DOWN": Keys.ARROW_DOWN,
        "ARROW_LEFT": Keys.ARROW_LEFT,
        "ARROW_RIGHT": Keys.ARROW_RIGHT,
    }

    def __init__(
        self,
        driver: WebDriver,
        screenshot_dir: str = "artifacts/screenshots",
        default_timeout: float = 15.0,
        platform: str = "",
        secret_resolver: SecretResolver | None = None,
    ):
        self._driver = driver
        self._screenshot_dir = Path(screenshot_dir)
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._default_timeout = default_timeout
        self._platform = platform.lower() or str(
            driver.capabilities.get("platformName", "")
        ).lower()
        self._secret_resolver = secret_resolver or SecretResolver()

    @property
    def driver(self) -> WebDriver:
        """Return the underlying Appium WebDriver instance."""
        return self._driver

    def _ensure_app_foreground(self):
        """Ensure the target mobile application is in the foreground."""
        if self._platform != "android":
            return
        try:
            curr_pkg = getattr(self._driver, "current_package", None)
            if not curr_pkg:
                return
            launcher_packages = (
                "com.google.android.apps.nexuslauncher",
                "com.android.launcher3",
                "com.android.systemui",
            )
            if curr_pkg in launcher_packages or "launcher" in curr_pkg.lower():
                target_pkg = (
                    self._driver.capabilities.get("appPackage")
                    or self._driver.capabilities.get("appium:appPackage")
                )
                if not target_pkg:
                    return
                logger.warning(
                    "Detected launcher/background (%s). Re-activating %s...",
                    curr_pkg,
                    target_pkg,
                )
                self._driver.activate_app(target_pkg)
                time.sleep(1.0)
        except Exception as e:
            logger.debug("Foreground check error (non-fatal): %s", sanitize_text(str(e)))

    def execute(self, action: Action) -> dict[str, Any]:
        """Execute a single structured action.

        Returns:
            A dict with keys: success, actual, error, classification, duration

        Raises:
            ActionExecutionError: If the action fails.
        """
        # Reject web-only actions on native apps
        if action.action_type in WEB_ONLY_ACTIONS:
            raise ActionExecutionError(
                f"Action '{action.action_type.value}' is not supported on native apps. "
                f"This action is web-only and cannot be executed via Appium.",
                classification=FailureClassification.AI_PLAN_ERROR,
                action=action,
            )

        self._ensure_app_foreground()
        start = time.time()
        handler = self._get_handler(action.action_type)

        try:
            execution_action = replace(
                action,
                value=(
                    self._secret_resolver.resolve(action.value)
                    if action.value is not None
                    else None
                ),
                extra=self._secret_resolver.resolve_data(action.extra),
            )
            actual = handler(execution_action)
            duration = time.time() - start
            return {
                "success": True,
                "actual": sanitize_text(str(actual)) if actual else "",
                "error": None,
                "classification": None,
                "duration": round(duration, 3),
            }
        except SecretResolutionError as e:
            raise ActionExecutionError(
                str(e),
                classification=FailureClassification.TEST_DATA_ERROR,
                action=action,
            ) from e
        except ActionExecutionError:
            raise
        except TimeoutException as e:
            raise ActionExecutionError(
                f"Timeout executing {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.TIMEOUT,
                action=action,
            ) from e
        except NoSuchElementException as e:
            raise ActionExecutionError(
                f"Element not found for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.LOCATOR_ERROR,
                action=action,
            ) from e
        except StaleElementReferenceException as e:
            raise ActionExecutionError(
                f"Stale element for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.AUTOMATION_ERROR,
                action=action,
            ) from e
        except WebDriverException as e:
            raise ActionExecutionError(
                f"WebDriver error for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.ENVIRONMENT_ERROR,
                action=action,
            ) from e
        except Exception as e:
            raise ActionExecutionError(
                f"Unexpected error for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.UNKNOWN,
                action=action,
            ) from e

    def capture_screenshot(self, name: str = "failure") -> str | None:
        """Capture a screenshot and return the file path."""
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_name = sanitize_filename_component(name, default="failure")
            filename = f"{safe_name}_{timestamp}.png"
            filepath = self._screenshot_dir / filename
            self._driver.save_screenshot(str(filepath))
            logger.info("Screenshot saved: %s", filepath)
            return str(filepath)
        except Exception:
            logger.exception("Failed to capture screenshot")
            return None

    def get_console_errors(self) -> list[str]:
        """Not applicable for native apps. Returns empty list."""
        return []

    def get_current_url(self) -> str:
        """Not applicable for native apps. Returns empty string."""
        return ""

    # ------------------------------------------------------------------
    # Internal: action dispatch
    # ------------------------------------------------------------------

    def _get_handler(self, action_type: ActionType):
        """Return the handler method for the given action type."""
        handlers = {
            # Common actions
            ActionType.CLICK: self._handle_click,
            ActionType.TYPE: self._handle_type,
            ActionType.CLEAR: self._handle_clear,
            ActionType.WAIT: self._handle_wait,
            ActionType.WAIT_VISIBLE: self._handle_wait_visible,
            ActionType.WAIT_NOT_VISIBLE: self._handle_wait_not_visible,
            ActionType.PRESS_KEY: self._handle_press_key,
            ActionType.VERIFY_TEXT: self._handle_verify_text,
            ActionType.VERIFY_NOT_TEXT: self._handle_verify_not_text,
            ActionType.VERIFY_VISIBLE: self._handle_verify_visible,
            ActionType.VERIFY_NOT_VISIBLE: self._handle_verify_not_visible,
            ActionType.VERIFY_VALUE: self._handle_verify_value,
            ActionType.VERIFY_ENABLED: self._handle_verify_enabled,
            ActionType.VERIFY_DISABLED: self._handle_verify_disabled,
            ActionType.VERIFY_ELEMENT_ATTRIBUTE: self._handle_verify_element_attribute,
            ActionType.VERIFY_ELEMENT_COUNT: self._handle_verify_element_count,
            ActionType.CAPTURE_SCREENSHOT: self._handle_capture_screenshot,
            ActionType.SLEEP: self._handle_sleep,
            # Native-specific actions
            ActionType.TAP: self._handle_tap,
            ActionType.SWIPE: self._handle_swipe,
            ActionType.SCROLL: self._handle_scroll,
            ActionType.LONG_PRESS: self._handle_long_press,
            ActionType.LAUNCH_APP: self._handle_launch_app,
            ActionType.CLOSE_APP: self._handle_close_app,
            ActionType.ACTIVATE_APP: self._handle_activate_app,
            ActionType.BACKGROUND_APP: self._handle_background_app,
            ActionType.HIDE_KEYBOARD: self._handle_hide_keyboard,
        }
        handler = handlers.get(action_type)
        if handler is None:
            raise ActionExecutionError(
                f"Unsupported action type for Appium: {action_type.value}",
                classification=FailureClassification.AI_PLAN_ERROR,
            )
        return handler

    def _find_element(self, locator: Locator, timeout: float | None = None, fallbacks: list[Locator] | None = None):
        """Find an element using the given locator with a wait, trying fallbacks if needed."""
        timeout = timeout or self._default_timeout
        all_locators = [locator] + (fallbacks or [])

        # Add Android-specific fallbacks only for Android sessions.
        extra_fallbacks = []
        for loc in (all_locators if self._platform == "android" else []):
            val_lower = loc.value.lower()
            if loc.strategy == LocatorStrategy.ACCESSIBILITY_ID:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    f"//*[@content-desc='{loc.value}' or @text='{loc.value}' or contains(translate(@content-desc,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{val_lower}') or contains(translate(@text,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{val_lower}')]"
                ))
            if "justpaid" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//*[contains(@text, 'justpaid') or contains(@content-desc, 'justpaid') or contains(@text, 'ljustpaid') or contains(@text, 'Ijustpaid')]"
                ))
            if loc.value in ("Home", "Search", "Messages", "Profile"):
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    f"//android.view.ViewGroup[@content-desc='{loc.value}'] | //android.widget.TextView[@text='{loc.value}']"
                ))
            if loc.value in ("+", "create", "Tap Center + Button") or "+" in loc.value or "1392" in loc.value or "horcrux" in loc.value:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.view.ViewGroup[contains(@bounds, '1392') and @clickable='true']"
                ))
            if "edit profile" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//*[@content-desc='Edit Profile' or @text='Edit Profile' or contains(@text, 'Edit Profile')]"
                ))
            if "eye" in val_lower or "toggle" in val_lower or "visibility" in val_lower or "viewgroup[1]" in val_lower or "viewgroup" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.view.ViewGroup[contains(@content-desc, '👁') or @content-desc='👁️\u200d🗨️'] | //android.widget.TextView[contains(@text, '👁') or @text='👁️\u200d🗨️'] | //*[@content-desc='eye' or contains(@content-desc, 'password')]"
                ))
            if "@password" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.widget.EditText[2]"
                ))
        all_locators.extend(extra_fallbacks)

        # Pass 1: Try finding without scrolling
        for loc in all_locators:
            try:
                by_type, value_fn = self._BY_MAP[loc.strategy]
                selector_value = value_fn(loc.value)
                element = WebDriverWait(self._driver, min(timeout, 3.0) if len(all_locators) > 1 else min(timeout, 4.0)).until(
                    EC.presence_of_element_located((by_type, selector_value))
                )
                if loc is not locator:
                    logger.info(
                        "Found element using fallback locator: %s=%s",
                        loc.strategy.value,
                        sanitize_text(loc.value),
                    )
                return element
            except (TimeoutException, NoSuchElementException):
                continue

        # Check if eligible for scroll-into-view
        val_lower = locator.value.strip().lower()
        is_fixed_element = (
            locator.value in ("Home", "Search", "Messages", "Profile", "+", "create", "Tap Center + Button")
            or "@content-desc='Home'" in locator.value
            or "@content-desc='Search'" in locator.value
            or "@content-desc='Messages'" in locator.value
            or "@content-desc='Profile'" in locator.value
            or "justpaid" in val_lower
        )

        if self._platform in {"android", "ios"} and not is_fixed_element:
            try:
                window_size = self._driver.get_window_size()
                width = window_size["width"]
                height = window_size["height"]
                self._driver.swipe(width // 2, int(height * 0.70), width // 2, int(height * 0.35), 400)
                time.sleep(0.5)

                for loc in all_locators:
                    try:
                        by_type, value_fn = self._BY_MAP[loc.strategy]
                        selector_value = value_fn(loc.value)
                        element = WebDriverWait(self._driver, 2.0).until(
                            EC.presence_of_element_located((by_type, selector_value))
                        )
                        return element
                    except (TimeoutException, NoSuchElementException):
                        continue

                self._driver.swipe(width // 2, int(height * 0.35), width // 2, int(height * 0.70), 400)
                time.sleep(0.5)
            except Exception:
                pass

        # All locators failed
        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return WebDriverWait(self._driver, 0.1).until(
            EC.presence_of_element_located((by_type, selector_value))
        )

    def _find_visible_element(self, locator: Locator, timeout: float | None = None, fallbacks: list[Locator] | None = None):
        """Find a visible element using the given locator, trying fallbacks if needed."""
        timeout = timeout or self._default_timeout
        all_locators = [locator] + (fallbacks or [])

        # Add Android-specific fallbacks only for Android sessions.
        extra_fallbacks = []
        for loc in (all_locators if self._platform == "android" else []):
            val_lower = loc.value.lower()
            if loc.strategy == LocatorStrategy.ACCESSIBILITY_ID:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    f"//*[@content-desc='{loc.value}' or @text='{loc.value}' or contains(translate(@content-desc,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{val_lower}') or contains(translate(@text,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{val_lower}')]"
                ))
            if "justpaid" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//*[contains(@text, 'justpaid') or contains(@content-desc, 'justpaid') or contains(@text, 'ljustpaid') or contains(@text, 'Ijustpaid')]"
                ))
            if loc.value in ("Home", "Search", "Messages", "Profile"):
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    f"//android.view.ViewGroup[@content-desc='{loc.value}'] | //android.widget.TextView[@text='{loc.value}']"
                ))
            if loc.value in ("+", "create", "Tap Center + Button") or "+" in loc.value or "1392" in loc.value or "horcrux" in loc.value:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.view.ViewGroup[contains(@bounds, '1392') and @clickable='true']"
                ))
            if "edit profile" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//*[@content-desc='Edit Profile' or @text='Edit Profile' or contains(@text, 'Edit Profile')]"
                ))
            if "eye" in val_lower or "toggle" in val_lower or "visibility" in val_lower or "viewgroup[1]" in val_lower or "viewgroup" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.view.ViewGroup[contains(@content-desc, '👁') or @content-desc='👁️\u200d🗨️'] | //android.widget.TextView[contains(@text, '👁') or @text='👁️\u200d🗨️'] | //*[@content-desc='eye' or contains(@content-desc, 'password')]"
                ))
            if "@password" in val_lower:
                extra_fallbacks.append(Locator(
                    LocatorStrategy.XPATH,
                    "//android.widget.EditText[2]"
                ))
        all_locators.extend(extra_fallbacks)

        # Pass 1: Try finding without scrolling
        for loc in all_locators:
            try:
                by_type, value_fn = self._BY_MAP[loc.strategy]
                selector_value = value_fn(loc.value)
                element = WebDriverWait(self._driver, min(timeout, 3.0) if len(all_locators) > 1 else min(timeout, 4.0)).until(
                    EC.visibility_of_element_located((by_type, selector_value))
                )
                if loc is not locator:
                    logger.info("Found visible element using fallback locator: %s=%s", loc.strategy.value, loc.value)
                return element
            except (TimeoutException, NoSuchElementException):
                continue

        # Check if eligible for scroll-into-view
        val_lower = locator.value.strip().lower()
        is_fixed_element = (
            locator.value in ("Home", "Search", "Messages", "Profile", "+", "create", "Tap Center + Button")
            or "@content-desc='Home'" in locator.value
            or "@content-desc='Search'" in locator.value
            or "@content-desc='Messages'" in locator.value
            or "@content-desc='Profile'" in locator.value
            or "justpaid" in val_lower
        )

        if self._platform in {"android", "ios"} and not is_fixed_element:
            try:
                window_size = self._driver.get_window_size()
                width = window_size["width"]
                height = window_size["height"]
                # Gentle scroll up (finger drags from 70% to 35%) to reveal content below fold
                self._driver.swipe(width // 2, int(height * 0.70), width // 2, int(height * 0.35), 400)
                time.sleep(0.5)

                for loc in all_locators:
                    try:
                        by_type, value_fn = self._BY_MAP[loc.strategy]
                        selector_value = value_fn(loc.value)
                        element = WebDriverWait(self._driver, min(timeout, 2.5) if len(all_locators) > 1 else min(timeout, 3.5)).until(
                            EC.visibility_of_element_located((by_type, selector_value))
                        )
                        logger.info(
                            "Found visible element after scroll: %s=%s",
                            loc.strategy.value,
                            sanitize_text(loc.value),
                        )
                        return element
                    except (TimeoutException, NoSuchElementException):
                        continue

                # If still not found, try gentle scroll down (finger drags from 35% to 70%) in case it was above
                self._driver.swipe(width // 2, int(height * 0.35), width // 2, int(height * 0.70), 400)
                time.sleep(0.5)
            except Exception as e:
                logger.debug("Scroll attempt error: %s", sanitize_text(str(e)))

        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return WebDriverWait(self._driver, 0.5).until(
            EC.visibility_of_element_located((by_type, selector_value))
        )

    def _find_elements(self, locator: Locator):
        """Find all elements matching the locator."""
        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return self._driver.find_elements(by_type, selector_value)

    # ------------------------------------------------------------------
    # Handler implementations: common actions
    # ------------------------------------------------------------------

    def _handle_click(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        try:
            element.click()
        except WebDriverException:
            # If element is covered (e.g. by soft keyboard), try hiding keyboard and retrying
            try:
                if hasattr(self._driver, "is_keyboard_shown"):
                    if self._driver.is_keyboard_shown():
                        self._driver.hide_keyboard()
                else:
                    self._driver.hide_keyboard()
                element.click()
            except WebDriverException:
                raise

    def _handle_type(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        try:
            element.click()
        except WebDriverException:
            pass
        if action.extra.get("clear_first", True):
            try:
                element.clear()
            except WebDriverException:
                pass
        element.send_keys(action.value or "")
        try:
            if hasattr(self._driver, "is_keyboard_shown"):
                if self._driver.is_keyboard_shown():
                    self._driver.hide_keyboard()
            else:
                self._driver.hide_keyboard()
        except WebDriverException:
            pass
        self._ensure_app_foreground()

    def _handle_clear(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        element.clear()

    def _handle_wait(self, action: Action):
        seconds = float(action.value or action.timeout)
        time.sleep(seconds)

    def _handle_wait_visible(self, action: Action):
        assert action.locator is not None
        self._find_visible_element(action.locator, action.timeout, action.fallback_locators)

    def _handle_wait_not_visible(self, action: Action):
        assert action.locator is not None
        timeout = action.timeout
        by_type, value_fn = self._BY_MAP[action.locator.strategy]
        selector_value = value_fn(action.locator.value)
        WebDriverWait(self._driver, timeout).until(
            EC.invisibility_of_element_located((by_type, selector_value))
        )

    def _handle_press_key(self, action: Action):
        key_name = (action.value or "").upper()
        if self._platform == "android" and key_name in ("BACK", "ANDROID_BACK", "KEY_BACK"):
            if hasattr(self._driver, "press_keycode"):
                self._driver.press_keycode(4)
                time.sleep(0.5)
                self._ensure_app_foreground()
                return "Pressed BACK key"
        key = self._KEY_MAP.get(key_name)
        if key is None:
            raise ActionExecutionError(
                f"Unknown key: {action.value}",
                classification=FailureClassification.AI_PLAN_ERROR,
                action=action,
            )
        if action.locator:
            element = self._find_visible_element(action.locator, action.timeout)
            element.send_keys(key)
        else:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(self._driver).send_keys(key).perform()

    def _handle_verify_text(self, action: Action):
        assert action.value is not None
        expected = action.value
        if action.locator:
            element = self._find_visible_element(action.locator, action.timeout)
            actual = element.text or element.get_attribute("text") or element.get_attribute("label") or ""
            if expected not in actual:
                raise ActionExecutionError(
                    f"Expected text '{expected}' not found in element. Actual: '{actual}'",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        else:
            page_source = self._driver.page_source
            if expected not in page_source:
                raise ActionExecutionError(
                    f"Expected text '{expected}' not found in page source",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        return f"Text '{expected}' found"

    def _handle_verify_not_text(self, action: Action):
        assert action.value is not None
        unexpected = action.value
        if action.locator:
            element = self._find_element(action.locator, action.timeout)
            actual = element.text or element.get_attribute("text") or element.get_attribute("label") or ""
            if unexpected in actual:
                raise ActionExecutionError(
                    f"Unexpected text '{unexpected}' found in element",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        else:
            if unexpected in self._driver.page_source:
                raise ActionExecutionError(
                    f"Unexpected text '{unexpected}' found in page source",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )

    def _handle_verify_visible(self, action: Action):
        assert action.locator is not None
        try:
            self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        except TimeoutException:
            raise ActionExecutionError(
                f"Element not visible: {action.locator.value}",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )

    def _handle_verify_not_visible(self, action: Action):
        assert action.locator is not None
        by_type, value_fn = self._BY_MAP[action.locator.strategy]
        selector_value = value_fn(action.locator.value)
        try:
            WebDriverWait(self._driver, action.timeout).until(
                EC.invisibility_of_element_located((by_type, selector_value))
            )
        except TimeoutException:
            raise ActionExecutionError(
                f"Element is still visible: {action.locator.value}",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )

    def _handle_verify_value(self, action: Action):
        assert action.value is not None
        assert action.locator is not None
        element = self._find_element(action.locator, action.timeout)
        actual = element.get_attribute("value") or element.get_attribute("text") or element.text
        expected = action.value
        if actual != expected:
            raise ActionExecutionError(
                f"Value mismatch. Expected: '{expected}', Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_enabled(self, action: Action):
        assert action.locator is not None
        element = self._find_element(action.locator, action.timeout)
        if not element.is_enabled():
            raise ActionExecutionError(
                f"Element is not enabled: {action.locator.value}",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )

    def _handle_verify_disabled(self, action: Action):
        assert action.locator is not None
        element = self._find_element(action.locator, action.timeout)
        if element.is_enabled():
            raise ActionExecutionError(
                f"Element is not disabled: {action.locator.value}",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )

    def _handle_verify_element_attribute(self, action: Action):
        assert action.value is not None
        assert action.locator is not None
        element = self._find_element(action.locator, action.timeout)
        attr_name = action.extra.get("attribute", "")
        expected = action.value
        actual = element.get_attribute(attr_name)
        if actual != expected:
            raise ActionExecutionError(
                f"Attribute '{attr_name}' mismatch. Expected: '{expected}', Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_element_count(self, action: Action):
        assert action.value is not None
        assert action.locator is not None
        elements = self._find_elements(action.locator)
        actual_count = len(elements)
        expected_count = int(action.value)
        op = action.extra.get("operator", "eq")
        success = False
        if op == "eq":
            success = actual_count == expected_count
        elif op == "gt":
            success = actual_count > expected_count
        elif op == "gte":
            success = actual_count >= expected_count
        elif op == "lt":
            success = actual_count < expected_count
        elif op == "lte":
            success = actual_count <= expected_count
        if not success:
            raise ActionExecutionError(
                f"Element count {op} check failed. Expected: {expected_count}, Actual: {actual_count}",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual_count

    def _handle_capture_screenshot(self, action: Action):
        name = action.value or "screenshot"
        path = self.capture_screenshot(name)
        return path

    def _handle_sleep(self, action: Action):
        seconds = float(action.value or 1)
        time.sleep(seconds)

    # ------------------------------------------------------------------
    # Handler implementations: native-specific actions
    # ------------------------------------------------------------------

    def _handle_tap(self, action: Action):
        """Tap on an element or at coordinates."""
        if action.locator:
            element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
            element.click()
        elif action.extra.get("x") is not None and action.extra.get("y") is not None:
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.actions.action_builder import ActionBuilder
            from selenium.webdriver.common.actions.pointer_input import PointerInput
            from selenium.webdriver.common.actions import interaction

            x, y = int(action.extra["x"]), int(action.extra["y"])
            actions = ActionChains(self._driver)
            actions.w3c_actions = ActionBuilder(self._driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
            actions.w3c_actions.pointer_action.move_to_location(x, y)
            actions.w3c_actions.pointer_action.pointer_down()
            actions.w3c_actions.pointer_action.pause(0.1)
            actions.w3c_actions.pointer_action.pointer_up()
            actions.perform()
        else:
            raise ActionExecutionError(
                "TAP requires either a locator or x/y coordinates in extra",
                classification=FailureClassification.AI_PLAN_ERROR,
                action=action,
            )

    def _handle_swipe(self, action: Action):
        """Swipe from one point to another."""
        start_x = int(action.extra.get("start_x", 500))
        start_y = int(action.extra.get("start_y", 1500))
        end_x = int(action.extra.get("end_x", 500))
        end_y = int(action.extra.get("end_y", 500))
        duration = int(action.extra.get("duration_ms", 800))
        self._driver.swipe(start_x, start_y, end_x, end_y, duration)

    def _handle_scroll(self, action: Action):
        """Scroll in a direction or to an element."""
        direction = action.extra.get("direction", "down")
        if action.locator:
            # Scroll to element
            element = self._find_element(action.locator, action.timeout)
            self._driver.execute_script("mobile: scroll", {"element": element, "direction": direction})
        else:
            # Generic scroll by swiping
            size = self._driver.get_window_size()
            mid_x = size["width"] // 2
            if direction == "down":
                self._driver.swipe(mid_x, size["height"] * 3 // 4, mid_x, size["height"] // 4, 800)
            elif direction == "up":
                self._driver.swipe(mid_x, size["height"] // 4, mid_x, size["height"] * 3 // 4, 800)

    def _handle_long_press(self, action: Action):
        """Long press on an element."""
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        duration_ms = int(action.extra.get("duration_ms", 1000))
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        actions = ActionChains(self._driver)
        actions.w3c_actions = ActionBuilder(self._driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
        actions.w3c_actions.pointer_action.move_to(element)
        actions.w3c_actions.pointer_action.pointer_down()
        actions.w3c_actions.pointer_action.pause(duration_ms / 1000.0)
        actions.w3c_actions.pointer_action.pointer_up()
        actions.perform()

    def _handle_launch_app(self, action: Action):
        """Launch or re-launch the app."""
        bundle_id = action.extra.get("bundle_id") or action.value
        if bundle_id:
            self._driver.activate_app(bundle_id)
        else:
            # Re-launch by resetting the session
            self._driver.launch_app()

    def _handle_close_app(self, action: Action):
        """Close the app."""
        bundle_id = action.extra.get("bundle_id") or action.value
        if bundle_id:
            self._driver.terminate_app(bundle_id)
        else:
            self._driver.close_app()

    def _handle_activate_app(self, action: Action):
        """Bring the app to the foreground."""
        assert action.value is not None, "ACTIVATE_APP requires a bundle_id/package in value"
        self._driver.activate_app(action.value)

    def _handle_background_app(self, action: Action):
        """Send the app to the background."""
        seconds = int(action.extra.get("duration", action.value or 5))
        self._driver.background_app(seconds)

    def _handle_hide_keyboard(self, action: Action):
        """Hide the on-screen keyboard."""
        try:
            self._driver.hide_keyboard()
        except WebDriverException:
            # Keyboard may not be visible; ignore
            pass
