"""Focused tests for symbolic secret planning and execution-time resolution."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from automation.actions.appium_executor import AppiumExecutor
from automation.actions.executor import ActionExecutionError, SeleniumExecutor
from automation.actions.schema import (
    Action,
    ActionType,
    Locator,
    LocatorStrategy,
    TestCase,
    TestResult,
    TestStatus,
)
from automation.ai.suite_parser import PageContext, TestCategory, TestInstruction
from automation.ai.test_generator import TestGenerator
from automation.secrets.resolver import SecretResolutionError, SecretResolver
from automation.secrets.sanitizer import REDACTED, sanitize_data


FAKE_SECRET = "SUPER_SECRET_TEST_VALUE_92841"
PASSWORD_PLACEHOLDER = "{{VALID_USER_PASSWORD}}"
EMAIL_PLACEHOLDER = "{{VALID_USER_EMAIL}}"


def test_secret_resolver_resolves_exact_and_embedded_placeholders():
    resolver = SecretResolver({"VALID_USER_PASSWORD": FAKE_SECRET})

    assert resolver.is_secret_placeholder(PASSWORD_PLACEHOLDER)
    assert resolver.resolve(PASSWORD_PLACEHOLDER) == FAKE_SECRET
    assert resolver.resolve(f"prefix-{PASSWORD_PLACEHOLDER}-suffix") == (
        f"prefix-{FAKE_SECRET}-suffix"
    )


def test_secret_resolver_missing_secret_fails_cleanly():
    resolver = SecretResolver({})

    with pytest.raises(SecretResolutionError, match="VALID_USER_PASSWORD") as exc:
        resolver.resolve(PASSWORD_PLACEHOLDER)

    assert FAKE_SECRET not in str(exc.value)


def test_gemini_prompt_never_contains_supplied_password(monkeypatch):
    monkeypatch.setenv("REGENERATE_TESTS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["prompt"] = kwargs["contents"]
            return SimpleNamespace(text="[]")

    generator = TestGenerator(
        base_url="https://example.invalid",
        email="fake-user@example.invalid",
        password=FAKE_SECRET,
        target_type="web",
    )
    generator._client = SimpleNamespace(models=FakeModels())
    page = PageContext(page_name="Login", page_number=1, url_pattern="/login")
    category = TestCategory(
        category_id="1.1",
        category_name="Authentication",
        instructions=[TestInstruction("Sign in with valid credentials")],
    )

    generator._generate_with_ai(page, category)

    assert FAKE_SECRET not in captured["prompt"]
    assert PASSWORD_PLACEHOLDER in captured["prompt"]
    assert EMAIL_PLACEHOLDER in captured["prompt"]


def test_action_serialization_and_cache_do_not_expose_secret(monkeypatch):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    action = Action(
        ActionType.TYPE,
        "Enter password",
        locator=Locator(LocatorStrategy.ID, "password"),
        value=FAKE_SECRET,
    )

    assert FAKE_SECRET not in json.dumps(action.to_dict())
    assert FAKE_SECRET not in json.dumps(action.to_cache_dict())


def test_testcase_and_result_serialization_do_not_expose_secret(monkeypatch):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    action = Action(
        ActionType.TYPE,
        "Enter password",
        locator=Locator(LocatorStrategy.ID, "password"),
        value=FAKE_SECRET,
    )
    test_case = TestCase(
        test_id="TC-SECRET-001",
        page="Login",
        functionality="Authentication",
        scenario=f"Authenticate with {FAKE_SECRET}",
        test_data={"password": FAKE_SECRET, "normal": "unchanged"},
        steps=[action],
    )
    result = TestResult(
        test_id="TC-SECRET-001",
        test_name="Secret failure",
        status=TestStatus.FAIL,
        actual=FAKE_SECRET,
        failure_reason=f"Driver echoed {FAKE_SECRET}",
    )

    assert FAKE_SECRET not in json.dumps(test_case.to_dict())
    assert FAKE_SECRET not in json.dumps(test_case.to_cache_dict())
    assert FAKE_SECRET not in json.dumps(result.to_dict())
    assert test_case.to_dict()["test_data"]["normal"] == "unchanged"


def test_cache_writer_sanitizes_before_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("REGENERATE_TESTS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    generator = TestGenerator(base_url="https://example.invalid", target_type="web")
    generator._cache_dir = tmp_path
    generator._cache_file = tmp_path / "new-cache.json"
    generator._cache = {"case": {"password": FAKE_SECRET, "normal": "unchanged"}}

    generator._save_cache()
    written = generator._cache_file.read_text(encoding="utf-8")

    assert FAKE_SECRET not in written
    assert json.loads(written)["case"]["normal"] == "unchanged"


@pytest.mark.parametrize("key", ["password", "api_token", "authorization", "secret"])
def test_sanitizer_recognizes_sensitive_keys(key):
    sanitized = sanitize_data({key: FAKE_SECRET, "normal": "unchanged"}, environ={})

    assert sanitized[key] == REDACTED
    assert sanitized["normal"] == "unchanged"


class _FakeElement:
    def __init__(self):
        self.sent = []
        self.cleared = False

    def click(self):
        return None

    def clear(self):
        self.cleared = True

    def send_keys(self, value):
        self.sent.append(value)


class _FakeDriver:
    capabilities = {"platformName": "iOS"}

    def hide_keyboard(self):
        return None


def test_selenium_resolves_secret_only_during_type(monkeypatch, tmp_path):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    element = _FakeElement()
    executor = SeleniumExecutor(_FakeDriver(), screenshot_dir=str(tmp_path))
    executor._find_visible_element = lambda *args, **kwargs: element
    action = Action(
        ActionType.TYPE,
        "Enter password",
        locator=Locator(LocatorStrategy.ID, "password"),
        value=PASSWORD_PLACEHOLDER,
    )

    executor.execute(action)

    assert element.sent == [FAKE_SECRET]
    assert action.value == PASSWORD_PLACEHOLDER


def test_appium_resolves_secret_only_during_type(monkeypatch, tmp_path):
    monkeypatch.setenv("VALID_USER_PASSWORD", FAKE_SECRET)
    element = _FakeElement()
    executor = AppiumExecutor(
        _FakeDriver(), screenshot_dir=str(tmp_path), platform="ios"
    )
    executor._find_visible_element = lambda *args, **kwargs: element
    action = Action(
        ActionType.TYPE,
        "Enter password",
        locator=Locator(LocatorStrategy.ACCESSIBILITY_ID, "password"),
        value=PASSWORD_PLACEHOLDER,
    )

    executor.execute(action)

    assert element.sent == [FAKE_SECRET]
    assert action.value == PASSWORD_PLACEHOLDER


def test_executor_reports_unresolved_secret_without_value(tmp_path):
    executor = SeleniumExecutor(
        _FakeDriver(),
        screenshot_dir=str(tmp_path),
        secret_resolver=SecretResolver({}),
    )
    action = Action(
        ActionType.TYPE,
        "Enter password",
        locator=Locator(LocatorStrategy.ID, "password"),
        value=PASSWORD_PLACEHOLDER,
    )

    with pytest.raises(ActionExecutionError, match="VALID_USER_PASSWORD") as exc:
        executor.execute(action)

    assert FAKE_SECRET not in str(exc.value)
