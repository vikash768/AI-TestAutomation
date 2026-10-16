"""
Structured Action Schema for AI-driven test automation.

Defines the deterministic action vocabulary that the AI test planner generates
and the Selenium executor consumes. The AI reasons about WHAT to test;
actions define HOW browser interactions are performed.

Actions are data-only (no Selenium imports). The executor translates them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from automation.secrets.sanitizer import sanitize_data, sanitize_text, sanitize_value


class ActionType(enum.Enum):
    """Deterministic action types for browser and native app automation."""

    # --- Common actions (Selenium + Appium) ---
    NAVIGATE = "NAVIGATE"
    CLICK = "CLICK"
    TYPE = "TYPE"
    CLEAR = "CLEAR"
    SELECT = "SELECT"
    CHECK = "CHECK"
    UNCHECK = "UNCHECK"
    WAIT = "WAIT"
    WAIT_VISIBLE = "WAIT_VISIBLE"
    WAIT_NOT_VISIBLE = "WAIT_NOT_VISIBLE"
    WAIT_URL_CONTAINS = "WAIT_URL_CONTAINS"
    WAIT_URL_NOT_CONTAINS = "WAIT_URL_NOT_CONTAINS"
    PRESS_KEY = "PRESS_KEY"
    VERIFY_TEXT = "VERIFY_TEXT"
    VERIFY_NOT_TEXT = "VERIFY_NOT_TEXT"
    VERIFY_VISIBLE = "VERIFY_VISIBLE"
    VERIFY_NOT_VISIBLE = "VERIFY_NOT_VISIBLE"
    VERIFY_URL = "VERIFY_URL"
    VERIFY_URL_CONTAINS = "VERIFY_URL_CONTAINS"
    VERIFY_URL_NOT_CONTAINS = "VERIFY_URL_NOT_CONTAINS"
    VERIFY_VALUE = "VERIFY_VALUE"
    VERIFY_ENABLED = "VERIFY_ENABLED"
    VERIFY_DISABLED = "VERIFY_DISABLED"
    VERIFY_TITLE = "VERIFY_TITLE"
    VERIFY_TITLE_CONTAINS = "VERIFY_TITLE_CONTAINS"
    VERIFY_ELEMENT_ATTRIBUTE = "VERIFY_ELEMENT_ATTRIBUTE"
    VERIFY_ELEMENT_COUNT = "VERIFY_ELEMENT_COUNT"
    CAPTURE_SCREENSHOT = "CAPTURE_SCREENSHOT"
    EXECUTE_JS = "EXECUTE_JS"
    RELOAD_PAGE = "RELOAD_PAGE"
    GO_BACK = "GO_BACK"
    SLEEP = "SLEEP"
    CLOSE_BROWSER = "CLOSE_BROWSER"
    OPEN_BROWSER = "OPEN_BROWSER"
    SET_WINDOW_SIZE = "SET_WINDOW_SIZE"
    GET_CONSOLE_ERRORS = "GET_CONSOLE_ERRORS"

    # --- Native app actions (Appium only) ---
    TAP = "TAP"
    SWIPE = "SWIPE"
    SCROLL = "SCROLL"
    LONG_PRESS = "LONG_PRESS"
    LAUNCH_APP = "LAUNCH_APP"
    CLOSE_APP = "CLOSE_APP"
    ACTIVATE_APP = "ACTIVATE_APP"
    BACKGROUND_APP = "BACKGROUND_APP"
    HIDE_KEYBOARD = "HIDE_KEYBOARD"


class LocatorStrategy(enum.Enum):
    """Locator strategies in priority order (per locator policy)."""

    # --- Common strategies (Selenium + Appium) ---
    DATA_TESTID = "data-testid"
    ID = "id"
    NAME = "name"
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"
    LINK_TEXT = "link_text"
    PARTIAL_LINK_TEXT = "partial_link_text"
    TAG_NAME = "tag_name"
    CLASS_NAME = "class_name"

    # --- Appium-specific strategies ---
    ACCESSIBILITY_ID = "accessibility_id"
    ANDROID_UIAUTOMATOR = "android_uiautomator"
    IOS_PREDICATE = "ios_predicate"

    @classmethod
    def from_str(cls, value: Any) -> "LocatorStrategy":
        """Parse any strategy string or alias into a canonical LocatorStrategy."""
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "tag": cls.TAG_NAME,
            "tag_name": cls.TAG_NAME,
            "tagname": cls.TAG_NAME,
            "class": cls.CLASS_NAME,
            "class_name": cls.CLASS_NAME,
            "classname": cls.CLASS_NAME,
            "data_testid": cls.DATA_TESTID,
            "datatestid": cls.DATA_TESTID,
            "testid": cls.DATA_TESTID,
            "data_test_id": cls.DATA_TESTID,
            "css": cls.CSS,
            "css_selector": cls.CSS,
            "cssselector": cls.CSS,
            "id": cls.ID,
            "name": cls.NAME,
            "xpath": cls.XPATH,
            "text": cls.TEXT,
            "link": cls.LINK_TEXT,
            "link_text": cls.LINK_TEXT,
            "linktext": cls.LINK_TEXT,
            "partial_link": cls.PARTIAL_LINK_TEXT,
            "partial_link_text": cls.PARTIAL_LINK_TEXT,
            "partiallinktext": cls.PARTIAL_LINK_TEXT,
            "accessibility_id": cls.ACCESSIBILITY_ID,
            "accessibilityid": cls.ACCESSIBILITY_ID,
            "android_uiautomator": cls.ANDROID_UIAUTOMATOR,
            "uiautomator": cls.ANDROID_UIAUTOMATOR,
            "ios_predicate": cls.IOS_PREDICATE,
        }
        if raw in aliases:
            return aliases[raw]
        for member in cls:
            if member.value == raw or member.value.replace("-", "_") == raw:
                return member
        if raw.startswith(("//", "(")) or "xpath" in raw:
            return cls.XPATH
        return cls.CSS


# Actions that only make sense in a web browser context
WEB_ONLY_ACTIONS = frozenset({
    ActionType.NAVIGATE,
    ActionType.VERIFY_URL,
    ActionType.VERIFY_URL_CONTAINS,
    ActionType.VERIFY_URL_NOT_CONTAINS,
    ActionType.WAIT_URL_CONTAINS,
    ActionType.WAIT_URL_NOT_CONTAINS,
    ActionType.RELOAD_PAGE,
    ActionType.GO_BACK,
    ActionType.EXECUTE_JS,
    ActionType.GET_CONSOLE_ERRORS,
    ActionType.OPEN_BROWSER,
    ActionType.CLOSE_BROWSER,
    ActionType.SET_WINDOW_SIZE,
})

# Actions that only make sense in a native app context
NATIVE_ONLY_ACTIONS = frozenset({
    ActionType.TAP,
    ActionType.SWIPE,
    ActionType.SCROLL,
    ActionType.LONG_PRESS,
    ActionType.LAUNCH_APP,
    ActionType.CLOSE_APP,
    ActionType.ACTIVATE_APP,
    ActionType.BACKGROUND_APP,
    ActionType.HIDE_KEYBOARD,
})


@dataclass
class Locator:
    """Element locator with strategy and value."""

    strategy: LocatorStrategy
    value: str

    def to_selenium_locator(self) -> str:
        """Convert to SeleniumLibrary-compatible locator string.

        Appium-specific strategies return a simple 'strategy:value' fallback.
        """
        strategy_map = {
            LocatorStrategy.DATA_TESTID: lambda v: f'css:[data-testid="{v}"]',
            LocatorStrategy.ID: lambda v: f"id:{v}",
            LocatorStrategy.NAME: lambda v: f"name:{v}",
            LocatorStrategy.CSS: lambda v: f"css:{v}",
            LocatorStrategy.XPATH: lambda v: f"xpath:{v}",
            LocatorStrategy.TEXT: lambda v: f"xpath://*[contains(text(),'{v}')]",
            LocatorStrategy.LINK_TEXT: lambda v: f"link:{v}",
            LocatorStrategy.PARTIAL_LINK_TEXT: lambda v: f"partial link:{v}",
            LocatorStrategy.TAG_NAME: lambda v: f"tag:{v}",
            LocatorStrategy.CLASS_NAME: lambda v: f"class:{v}",
        }
        converter = strategy_map.get(self.strategy)
        if converter:
            return converter(self.value)
        # Fallback for Appium-specific strategies
        return f"{self.strategy.value}:{self.value}"


@dataclass
class Action:
    """A single deterministic browser action."""

    action_type: ActionType
    description: str = ""
    locator: Locator | None = None
    fallback_locators: list[Locator] = field(default_factory=list)
    value: str | None = None
    timeout: float = 15.0
    extra: dict[str, Any] = field(default_factory=dict)

    def _value_is_sensitive(self) -> bool:
        """Return whether the action value represents credential input."""
        if self.action_type != ActionType.TYPE:
            return False
        locator_value = self.locator.value if self.locator else ""
        context = f"{self.description} {locator_value}"
        return any(
            marker in context.lower()
            for marker in (
                "password", "passwd", "secret", "token", "api key",
                "api_key", "apikey", "authorization", "credential",
            )
        )

    def to_dict(self) -> dict:
        """Serialize action to dict for logging/reporting."""
        result: dict[str, Any] = {
            "action": self.action_type.value,
            "description": sanitize_text(self.description),
        }
        if self.locator:
            result["locator"] = {
                "strategy": self.locator.strategy.value,
                "value": sanitize_text(self.locator.value),
            }
        if self.value is not None:
            result["value"] = sanitize_value(
                self.value,
                sensitive=self._value_is_sensitive(),
            )
        if self.timeout != 15.0:
            result["timeout"] = self.timeout
        if self.extra:
            result["extra"] = sanitize_data(self.extra)
        return sanitize_data(result)

    def to_cache_dict(self) -> dict:
        """Serialize action to dict for persistent test caching."""
        result = {
            "action_type": self.action_type.value,
            "description": sanitize_text(self.description),
            "locator": {
                "strategy": self.locator.strategy.value,
                "value": sanitize_text(self.locator.value),
            } if self.locator else None,
            "fallback_locators": [
                {"strategy": fl.strategy.value, "value": sanitize_text(fl.value)}
                for fl in self.fallback_locators
            ],
            "value": sanitize_value(
                self.value,
                sensitive=self._value_is_sensitive(),
            ),
            "timeout": self.timeout,
            "extra": sanitize_data(self.extra),
        }
        return sanitize_data(result)

    @classmethod
    def from_cache_dict(cls, data: dict) -> "Action":
        """Reconstruct action from cache dict."""
        loc = None
        if data.get("locator"):
            loc = Locator(
                strategy=LocatorStrategy.from_str(data["locator"].get("strategy")),
                value=str(data["locator"].get("value", "")),
            )
        fallbacks = [
            Locator(
                strategy=LocatorStrategy.from_str(fl.get("strategy")),
                value=str(fl.get("value", "")),
            )
            for fl in data.get("fallback_locators", [])
            if fl and fl.get("strategy") and fl.get("value")
        ]
        return cls(
            action_type=ActionType(data["action_type"]),
            description=data.get("description", ""),
            locator=loc,
            fallback_locators=fallbacks,
            value=data.get("value"),
            timeout=float(data.get("timeout", 15.0)),
            extra=data.get("extra", {}),
        )


class TestStatus(enum.Enum):
    """Test execution status."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


class FailureClassification(enum.Enum):
    """Failure classification categories."""

    APPLICATION_BUG = "APPLICATION_BUG"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    API_ERROR = "API_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    LOCATOR_ERROR = "LOCATOR_ERROR"
    TIMEOUT = "TIMEOUT"
    AUTOMATION_ERROR = "AUTOMATION_ERROR"
    AI_PLAN_ERROR = "AI_PLAN_ERROR"
    TEST_DATA_ERROR = "TEST_DATA_ERROR"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class TestCase:
    """A generated test case with metadata and action steps."""

    test_id: str
    page: str
    functionality: str
    scenario: str
    test_name: str = ""
    preconditions: list[str] = field(default_factory=list)
    test_data: dict[str, str] = field(default_factory=dict)
    steps: list[Action] = field(default_factory=list)
    expected_result: str = ""
    priority: str = "P1"
    test_type: str = "browser"
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.test_name:
            self.test_name = self.scenario

    def to_dict(self) -> dict:
        """Serialize test case for logging/reporting."""
        return sanitize_data({
            "test_id": self.test_id,
            "test_name": self.test_name,
            "page": self.page,
            "functionality": self.functionality,
            "scenario": self.scenario,
            "preconditions": self.preconditions,
            "test_data": self.test_data,
            "steps": [s.to_dict() for s in self.steps],
            "expected_result": self.expected_result,
            "priority": self.priority,
            "test_type": self.test_type,
            "tags": self.tags,
        })

    def to_cache_dict(self) -> dict:
        """Serialize test case to dict for persistent test caching."""
        return sanitize_data({
            "test_id": self.test_id,
            "test_name": self.test_name,
            "page": self.page,
            "functionality": self.functionality,
            "scenario": self.scenario,
            "preconditions": self.preconditions,
            "test_data": self.test_data,
            "steps": [s.to_cache_dict() for s in self.steps],
            "expected_result": self.expected_result,
            "priority": self.priority,
            "test_type": self.test_type,
            "tags": self.tags,
        })

    @classmethod
    def from_cache_dict(cls, data: dict) -> "TestCase":
        """Reconstruct TestCase from cache dict."""
        return cls(
            test_id=data["test_id"],
            page=data["page"],
            functionality=data["functionality"],
            scenario=data["scenario"],
            test_name=data.get("test_name", data["scenario"]),
            preconditions=data.get("preconditions", []),
            test_data=data.get("test_data", {}),
            steps=[Action.from_cache_dict(s) for s in data.get("steps", [])],
            expected_result=data.get("expected_result", ""),
            priority=data.get("priority", "P1"),
            test_type=data.get("test_type", "browser"),
            tags=data.get("tags", []),
        )


@dataclass
class TestResult:
    """Result of executing a single test case."""

    test_id: str
    test_name: str
    status: TestStatus
    expected: str = ""
    actual: str = ""
    duration_seconds: float = 0.0
    failure_reason: str = ""
    failure_classification: FailureClassification | None = None
    screenshot_path: str | None = None
    console_errors: list[str] = field(default_factory=list)
    api_errors: list[str] = field(default_factory=list)
    error_details: str = ""
    page: str = ""
    functionality: str = ""
    url: str = ""
    browser: str = ""
    project: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        """Serialize result for logging/reporting."""
        result: dict[str, Any] = {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "status": self.status.value,
            "expected": self.expected,
            "actual": self.actual,
            "duration_seconds": round(self.duration_seconds, 3),
        }
        if self.failure_reason:
            result["failure_reason"] = self.failure_reason
        if self.failure_classification:
            result["failure_classification"] = self.failure_classification.value
        if self.screenshot_path:
            result["screenshot"] = self.screenshot_path
        if self.console_errors:
            result["console_errors"] = self.console_errors
        if self.api_errors:
            result["api_errors"] = self.api_errors
        if self.error_details:
            result["error_details"] = self.error_details
        result["page"] = self.page
        result["functionality"] = self.functionality
        result["url"] = self.url
        result["browser"] = self.browser
        result["project"] = self.project
        result["timestamp"] = self.timestamp
        return sanitize_data(result)
