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


class ActionType(enum.Enum):
    """Deterministic browser action types."""

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


class LocatorStrategy(enum.Enum):
    """Locator strategies in priority order (per locator policy)."""

    DATA_TESTID = "data-testid"
    ID = "id"
    NAME = "name"
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"
    LINK_TEXT = "link_text"
    PARTIAL_LINK_TEXT = "partial_link_text"


@dataclass
class Locator:
    """Element locator with strategy and value."""

    strategy: LocatorStrategy
    value: str

    def to_selenium_locator(self) -> str:
        """Convert to SeleniumLibrary-compatible locator string."""
        strategy_map = {
            LocatorStrategy.DATA_TESTID: lambda v: f'css:[data-testid="{v}"]',
            LocatorStrategy.ID: lambda v: f"id:{v}",
            LocatorStrategy.NAME: lambda v: f"name:{v}",
            LocatorStrategy.CSS: lambda v: f"css:{v}",
            LocatorStrategy.XPATH: lambda v: f"xpath:{v}",
            LocatorStrategy.TEXT: lambda v: f"xpath://*[contains(text(),'{v}')]",
            LocatorStrategy.LINK_TEXT: lambda v: f"link:{v}",
            LocatorStrategy.PARTIAL_LINK_TEXT: lambda v: f"partial link:{v}",
        }
        return strategy_map[self.strategy](self.value)


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

    def to_dict(self) -> dict:
        """Serialize action to dict for logging/reporting."""
        result: dict[str, Any] = {
            "action": self.action_type.value,
            "description": self.description,
        }
        if self.locator:
            result["locator"] = {
                "strategy": self.locator.strategy.value,
                "value": self.locator.value,
            }
        if self.value is not None:
            result["value"] = self.value
        if self.timeout != 15.0:
            result["timeout"] = self.timeout
        if self.extra:
            result["extra"] = self.extra
        return result


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
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "page": self.page,
            "functionality": self.functionality,
            "scenario": self.scenario,
            "preconditions": self.preconditions,
            "test_data": {k: v for k, v in self.test_data.items() if "password" not in k.lower()},
            "steps": [s.to_dict() for s in self.steps],
            "expected_result": self.expected_result,
            "priority": self.priority,
            "test_type": self.test_type,
            "tags": self.tags,
        }


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
        return result
