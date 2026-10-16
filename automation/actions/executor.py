"""
Selenium Action Executor.

Translates structured Action objects into deterministic Selenium WebDriver
commands. The AI reasons about WHAT to test; this executor handles HOW.

This executor uses SeleniumLibrary under the hood but wraps it to:
1. Provide uniform error handling and evidence collection
2. Map structured actions to Selenium calls deterministically
3. Classify failures into meaningful categories
4. Capture screenshots and console errors on failure
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    InvalidSelectorException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

from automation.actions.schema import (
    Action,
    ActionType,
    FailureClassification,
    Locator,
    LocatorStrategy,
)
from automation.secrets.resolver import SecretResolutionError, SecretResolver
from automation.secrets.sanitizer import sanitize_filename_component, sanitize_text

logger = logging.getLogger(__name__)


class ActionExecutionError(Exception):
    """Raised when an action fails to execute."""

    def __init__(
        self,
        message: str,
        classification: FailureClassification = FailureClassification.UNKNOWN,
        action: Action | None = None,
    ):
        super().__init__(message)
        self.classification = classification
        self.action = action


class SeleniumExecutor:
    """Executes structured Action objects via Selenium WebDriver.

    This class takes a live WebDriver instance and translates Action
    objects into deterministic browser commands.
    """

    # Map LocatorStrategy to Selenium By
    _BY_MAP = {
        LocatorStrategy.DATA_TESTID: (By.CSS_SELECTOR, lambda v: f'[data-testid="{v}"]'),
        LocatorStrategy.ID: (By.ID, lambda v: v),
        LocatorStrategy.NAME: (By.NAME, lambda v: v),
        LocatorStrategy.CSS: (By.CSS_SELECTOR, lambda v: v),
        LocatorStrategy.XPATH: (By.XPATH, lambda v: v),
        LocatorStrategy.TEXT: (By.XPATH, lambda v: f"//*[contains(text(),'{v}')]"),
        LocatorStrategy.LINK_TEXT: (By.LINK_TEXT, lambda v: v),
        LocatorStrategy.PARTIAL_LINK_TEXT: (By.PARTIAL_LINK_TEXT, lambda v: v),
        LocatorStrategy.TAG_NAME: (By.TAG_NAME, lambda v: v),
        LocatorStrategy.CLASS_NAME: (By.CLASS_NAME, lambda v: v),
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
        secret_resolver: SecretResolver | None = None,
    ):
        self._driver = driver
        self._screenshot_dir = Path(screenshot_dir)
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._default_timeout = default_timeout
        self._secret_resolver = secret_resolver or SecretResolver()

    @property
    def driver(self) -> WebDriver:
        """Return the underlying WebDriver instance."""
        return self._driver

    def execute(self, action: Action) -> dict[str, Any]:
        """Execute a single structured action.

        Returns:
            A dict with keys: success (bool), actual (str), error (str|None),
            classification (str|None), duration (float)

        Raises:
            ActionExecutionError: If the action fails in a way that should
            stop the test.
        """
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
            duration = time.time() - start
            raise ActionExecutionError(
                f"Timeout executing {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.TIMEOUT,
                action=action,
            ) from e
        except NoSuchElementException as e:
            duration = time.time() - start
            raise ActionExecutionError(
                f"Element not found for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.LOCATOR_ERROR,
                action=action,
            ) from e
        except ElementNotInteractableException as e:
            duration = time.time() - start
            raise ActionExecutionError(
                f"Element not interactable for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.LOCATOR_ERROR,
                action=action,
            ) from e
        except StaleElementReferenceException as e:
            duration = time.time() - start
            raise ActionExecutionError(
                f"Stale element for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.AUTOMATION_ERROR,
                action=action,
            ) from e
        except WebDriverException as e:
            duration = time.time() - start
            raise ActionExecutionError(
                f"WebDriver error for {action.action_type.value}: {sanitize_text(str(e))}",
                classification=FailureClassification.ENVIRONMENT_ERROR,
                action=action,
            ) from e
        except Exception as e:
            duration = time.time() - start
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
        """Get browser console errors (Chrome only)."""
        try:
            logs = self._driver.get_log("browser")  # type: ignore
            errors = [
                entry["message"]
                for entry in logs
                if entry.get("level") in ("SEVERE", "ERROR")
            ]
            return errors
        except Exception:
            # Not all browsers/configurations support log retrieval
            return []

    def get_current_url(self) -> str:
        """Get the current browser URL."""
        try:
            return self._driver.current_url
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Internal: action dispatch
    # ------------------------------------------------------------------

    def _get_handler(self, action_type: ActionType):
        """Return the handler method for the given action type."""
        handlers = {
            ActionType.NAVIGATE: self._handle_navigate,
            ActionType.CLICK: self._handle_click,
            ActionType.TYPE: self._handle_type,
            ActionType.CLEAR: self._handle_clear,
            ActionType.SELECT: self._handle_select,
            ActionType.CHECK: self._handle_check,
            ActionType.UNCHECK: self._handle_uncheck,
            ActionType.WAIT: self._handle_wait,
            ActionType.WAIT_VISIBLE: self._handle_wait_visible,
            ActionType.WAIT_NOT_VISIBLE: self._handle_wait_not_visible,
            ActionType.WAIT_URL_CONTAINS: self._handle_wait_url_contains,
            ActionType.WAIT_URL_NOT_CONTAINS: self._handle_wait_url_not_contains,
            ActionType.PRESS_KEY: self._handle_press_key,
            ActionType.VERIFY_TEXT: self._handle_verify_text,
            ActionType.VERIFY_NOT_TEXT: self._handle_verify_not_text,
            ActionType.VERIFY_VISIBLE: self._handle_verify_visible,
            ActionType.VERIFY_NOT_VISIBLE: self._handle_verify_not_visible,
            ActionType.VERIFY_URL: self._handle_verify_url,
            ActionType.VERIFY_URL_CONTAINS: self._handle_verify_url_contains,
            ActionType.VERIFY_URL_NOT_CONTAINS: self._handle_verify_url_not_contains,
            ActionType.VERIFY_VALUE: self._handle_verify_value,
            ActionType.VERIFY_ENABLED: self._handle_verify_enabled,
            ActionType.VERIFY_DISABLED: self._handle_verify_disabled,
            ActionType.VERIFY_TITLE: self._handle_verify_title,
            ActionType.VERIFY_TITLE_CONTAINS: self._handle_verify_title_contains,
            ActionType.VERIFY_ELEMENT_ATTRIBUTE: self._handle_verify_element_attribute,
            ActionType.VERIFY_ELEMENT_COUNT: self._handle_verify_element_count,
            ActionType.CAPTURE_SCREENSHOT: self._handle_capture_screenshot,
            ActionType.EXECUTE_JS: self._handle_execute_js,
            ActionType.RELOAD_PAGE: self._handle_reload_page,
            ActionType.GO_BACK: self._handle_go_back,
            ActionType.SLEEP: self._handle_sleep,
            ActionType.CLOSE_BROWSER: self._handle_close_browser,
            ActionType.OPEN_BROWSER: self._handle_open_browser,
            ActionType.SET_WINDOW_SIZE: self._handle_set_window_size,
            ActionType.GET_CONSOLE_ERRORS: self._handle_get_console_errors,
        }
        handler = handlers.get(action_type)
        if handler is None:
            raise ActionExecutionError(
                f"Unknown action type: {action_type}",
                classification=FailureClassification.AI_PLAN_ERROR,
            )
        return handler

    def _find_element(self, locator: Locator, timeout: float | None = None, fallbacks: list[Locator] | None = None):
        """Find an element using the given locator with a wait, trying fallbacks if needed."""
        timeout = timeout or self._default_timeout
        all_locators = [locator] + (fallbacks or [])
        last_error = None

        for loc in all_locators:
            try:
                by_type, value_fn = self._BY_MAP[loc.strategy]
                selector_value = value_fn(loc.value)
                element = WebDriverWait(self._driver, min(timeout, 5.0) if len(all_locators) > 1 else timeout).until(
                    EC.presence_of_element_located((by_type, selector_value))
                )
                if loc is not locator:
                    logger.info(
                        "Found element using fallback locator: %s=%s",
                        loc.strategy.value,
                        sanitize_text(loc.value),
                    )
                return element
            except (TimeoutException, NoSuchElementException, InvalidSelectorException) as e:
                last_error = e
                continue

        # All locators failed — raise with the original timeout for consistent error reporting
        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return WebDriverWait(self._driver, 0.1).until(
            EC.presence_of_element_located((by_type, selector_value))
        )

    def _find_visible_element(self, locator: Locator, timeout: float | None = None, fallbacks: list[Locator] | None = None):
        """Find a visible element using the given locator, trying fallbacks if needed."""
        timeout = timeout or self._default_timeout
        all_locators = [locator] + (fallbacks or [])
        last_error = None

        for loc in all_locators:
            try:
                by_type, value_fn = self._BY_MAP[loc.strategy]
                selector_value = value_fn(loc.value)
                element = WebDriverWait(self._driver, min(timeout, 5.0) if len(all_locators) > 1 else timeout).until(
                    EC.visibility_of_element_located((by_type, selector_value))
                )
                if loc is not locator:
                    logger.info(
                        "Found visible element using fallback locator: %s=%s",
                        loc.strategy.value,
                        sanitize_text(loc.value),
                    )
                return element
            except (TimeoutException, NoSuchElementException, InvalidSelectorException) as e:
                last_error = e
                continue

        # All locators failed — raise with the original for error reporting
        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return WebDriverWait(self._driver, 0.1).until(
            EC.visibility_of_element_located((by_type, selector_value))
        )

    def _find_elements(self, locator: Locator):
        """Find all elements matching the locator."""
        by_type, value_fn = self._BY_MAP[locator.strategy]
        selector_value = value_fn(locator.value)
        return self._driver.find_elements(by_type, selector_value)

    # ------------------------------------------------------------------
    # Handler implementations
    # ------------------------------------------------------------------

    def _handle_navigate(self, action: Action):
        assert action.value is not None
        self._driver.get(action.value)
        return self._driver.current_url

    def _handle_click(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        element.click()

    def _handle_type(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        if action.extra.get("clear_first", True):
            element.clear()
        element.send_keys(action.value or "")

    def _handle_clear(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        element.clear()

    def _handle_select(self, action: Action):
        assert action.locator is not None
        assert action.value is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        select = Select(element)
        by = action.extra.get("by", "visible_text")
        if by == "value":
            select.select_by_value(action.value)
        elif by == "index":
            select.select_by_index(int(action.value))
        else:
            select.select_by_visible_text(action.value)

    def _handle_check(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        if not element.is_selected():
            element.click()

    def _handle_uncheck(self, action: Action):
        assert action.locator is not None
        element = self._find_visible_element(action.locator, action.timeout, action.fallback_locators)
        if element.is_selected():
            element.click()

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

    def _handle_wait_url_contains(self, action: Action):
        assert action.value is not None
        WebDriverWait(self._driver, action.timeout).until(
            EC.url_contains(action.value)
        )

    def _handle_wait_url_not_contains(self, action: Action):
        assert action.value is not None
        WebDriverWait(self._driver, action.timeout).until(
            lambda driver: action.value not in driver.current_url
        )

    def _handle_press_key(self, action: Action):
        key_name = (action.value or "").upper()
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
            actual = element.text
            if expected not in actual:
                raise ActionExecutionError(
                    f"Expected text '{expected}' not found in element. Actual: '{actual}'",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        else:
            # Check entire page source
            if expected not in self._driver.page_source:
                raise ActionExecutionError(
                    f"Expected text '{expected}' not found on page",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        return f"Text '{expected}' found"

    def _handle_verify_not_text(self, action: Action):
        assert action.value is not None
        unexpected = action.value
        if action.locator:
            element = self._find_element(action.locator, action.timeout)
            actual = element.text
            if unexpected in actual:
                raise ActionExecutionError(
                    f"Unexpected text '{unexpected}' found in element",
                    classification=FailureClassification.VALIDATION_FAILURE,
                    action=action,
                )
        else:
            if unexpected in self._driver.page_source:
                raise ActionExecutionError(
                    f"Unexpected text '{unexpected}' found on page",
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

    def _handle_verify_url(self, action: Action):
        assert action.value is not None
        actual = self._driver.current_url
        expected = action.value
        if actual != expected:
            raise ActionExecutionError(
                f"URL mismatch. Expected: '{expected}', Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_url_contains(self, action: Action):
        assert action.value is not None
        actual = self._driver.current_url
        expected = action.value
        if expected not in actual:
            raise ActionExecutionError(
                f"URL does not contain '{expected}'. Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_url_not_contains(self, action: Action):
        assert action.value is not None
        actual = self._driver.current_url
        unexpected = action.value
        if unexpected in actual:
            raise ActionExecutionError(
                f"URL should not contain '{unexpected}'. Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_value(self, action: Action):
        assert action.value is not None
        assert action.locator is not None
        element = self._find_element(action.locator, action.timeout)
        actual = element.get_attribute("value")
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

    def _handle_verify_title(self, action: Action):
        assert action.value is not None
        actual = self._driver.title
        expected = action.value
        if actual != expected:
            raise ActionExecutionError(
                f"Title mismatch. Expected: '{expected}', Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

    def _handle_verify_title_contains(self, action: Action):
        assert action.value is not None
        actual = self._driver.title
        expected = action.value
        if expected not in actual:
            raise ActionExecutionError(
                f"Title does not contain '{expected}'. Actual: '{actual}'",
                classification=FailureClassification.VALIDATION_FAILURE,
                action=action,
            )
        return actual

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

    def _handle_execute_js(self, action: Action):
        assert action.value is not None
        result = self._driver.execute_script(action.value)
        return result

    def _handle_reload_page(self, action: Action):
        self._driver.refresh()

    def _handle_go_back(self, action: Action):
        self._driver.back()

    def _handle_sleep(self, action: Action):
        seconds = float(action.value or 1)
        time.sleep(seconds)

    def _handle_close_browser(self, action: Action):
        self._driver.quit()

    def _handle_open_browser(self, action: Action):
        # This is handled externally; open_browser just navigates
        if action.value:
            self._driver.get(action.value)

    def _handle_set_window_size(self, action: Action):
        width = action.extra.get("width", 1920)
        height = action.extra.get("height", 1080)
        self._driver.set_window_size(width, height)

    def _handle_get_console_errors(self, action: Action):
        errors = self.get_console_errors()
        if errors and action.extra.get("fail_on_errors", False):
            raise ActionExecutionError(
                f"Console errors found: {errors}",
                classification=FailureClassification.APPLICATION_BUG,
                action=action,
            )
        return errors
