"""Tests for Web Test Generator, LocatorStrategy normalization, and caching."""

import json
from pathlib import Path
import pytest

from automation.actions.schema import (
    Action,
    ActionType,
    Locator,
    LocatorStrategy,
    TestCase,
)
from automation.actions.executor import SeleniumExecutor
from automation.ai.test_generator import TestGenerator
from automation.ai.suite_parser import PageContext, TestCategory, TestInstruction


def test_locator_strategy_from_str():
    assert LocatorStrategy.from_str("tag_name") == LocatorStrategy.TAG_NAME
    assert LocatorStrategy.from_str("tag") == LocatorStrategy.TAG_NAME
    assert LocatorStrategy.from_str("tag-name") == LocatorStrategy.TAG_NAME
    assert LocatorStrategy.from_str("class_name") == LocatorStrategy.CLASS_NAME
    assert LocatorStrategy.from_str("class") == LocatorStrategy.CLASS_NAME
    assert LocatorStrategy.from_str("data-testid") == LocatorStrategy.DATA_TESTID
    assert LocatorStrategy.from_str("testid") == LocatorStrategy.DATA_TESTID
    assert LocatorStrategy.from_str("css") == LocatorStrategy.CSS
    assert LocatorStrategy.from_str("css_selector") == LocatorStrategy.CSS
    assert LocatorStrategy.from_str("xpath") == LocatorStrategy.XPATH
    assert LocatorStrategy.from_str("id") == LocatorStrategy.ID
    assert LocatorStrategy.from_str("name") == LocatorStrategy.NAME
    assert LocatorStrategy.from_str("text") == LocatorStrategy.TEXT
    assert LocatorStrategy.from_str("link_text") == LocatorStrategy.LINK_TEXT
    assert LocatorStrategy.from_str("partial_link_text") == LocatorStrategy.PARTIAL_LINK_TEXT


def test_to_selenium_locator_tag_and_class():
    loc_tag = Locator(LocatorStrategy.TAG_NAME, "button")
    assert loc_tag.to_selenium_locator() == "tag:button"

    loc_class = Locator(LocatorStrategy.CLASS_NAME, "submit-btn")
    assert loc_class.to_selenium_locator() == "class:submit-btn"


def test_selenium_executor_by_map_supports_tag_and_class():
    assert LocatorStrategy.TAG_NAME in SeleniumExecutor._BY_MAP
    assert LocatorStrategy.CLASS_NAME in SeleniumExecutor._BY_MAP


def test_action_and_testcase_cache_serialization():
    act = Action(
        action_type=ActionType.TYPE,
        description="Enter email",
        locator=Locator(LocatorStrategy.TAG_NAME, "input"),
        fallback_locators=[Locator(LocatorStrategy.ID, "email")],
        value="test@example.com",
        timeout=10.0,
    )
    act_cache = act.to_cache_dict()
    act_restored = Action.from_cache_dict(act_cache)
    assert act_restored.action_type == ActionType.TYPE
    assert act_restored.locator.strategy == LocatorStrategy.TAG_NAME
    assert act_restored.locator.value == "input"
    assert len(act_restored.fallback_locators) == 1
    assert act_restored.fallback_locators[0].strategy == LocatorStrategy.ID
    assert act_restored.value == "test@example.com"

    tc = TestCase(
        test_id="TC-TP-TEST-001",
        page="Login",
        functionality="Authentication",
        scenario="Valid login",
        steps=[act],
        expected_result="User dashboard is displayed",
        priority="P0",
        test_type="web",
    )
    tc_cache = tc.to_cache_dict()
    tc_restored = TestCase.from_cache_dict(tc_cache)
    assert tc_restored.test_id == "TC-TP-TEST-001"
    assert tc_restored.page == "Login"
    assert len(tc_restored.steps) == 1
    assert tc_restored.steps[0].value == "test@example.com"


def test_generator_caching(tmp_path, monkeypatch):
    monkeypatch.setenv("REGENERATE_TESTS", "1")
    monkeypatch.setenv("VALID_USER_EMAIL", "admin@example.com")
    monkeypatch.setenv("VALID_USER_PASSWORD", "Secret123")
    gen = TestGenerator(base_url="https://trackpilot.pods365.com", target_type="web")
    gen._client = None
    gen._cache_dir = tmp_path
    gen._cache_file = tmp_path / "test_cache.json"
    gen._cache = {}

    page = PageContext(page_name="Login", page_number=1, url_pattern="/login")
    cat = TestCategory(
        category_id="1.1",
        category_name="Authentication",
        instructions=[TestInstruction("Submit valid credentials")],
    )

    cases = gen._generate_category_tests(page, cat)
    assert len(cases) == 1
    assert gen._cache_file.exists()

    # Second call should load from cache
    loaded = gen._generate_category_tests(page, cat)
    assert len(loaded) == 1
    assert loaded[0].scenario == cases[0].scenario
