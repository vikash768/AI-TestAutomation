"""
Validation tests for Appium integration.

Verifies that:
1. Existing web target still selects SeleniumExecutor.
2. Desktop target selects Appium execution.
3. Android/mobile target selects Appium execution.
4. Missing web BASE_URL gives a meaningful error.
5. Missing Appium configuration gives a meaningful error.
6. Web-only actions are not incorrectly executed against native apps.
7. Existing Action/TestCase serialization remains compatible.
8. Schema extensions are backward-compatible.
"""

import os
import pytest
import sys
import zipfile

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.actions.schema import (
    Action,
    ActionType,
    FailureClassification,
    Locator,
    LocatorStrategy,
    TestCase,
    TestResult,
    TestStatus,
    WEB_ONLY_ACTIONS,
    NATIVE_ONLY_ACTIONS,
)
from automation.actions.executor import ActionExecutionError, SeleniumExecutor
from automation.ai.test_generator import TestGenerator
from automation.ai.suite_parser import PageContext, TestCategory, TestInstruction
from automation.runner import AITestRunner, VALID_TARGET_TYPES, VALID_APPIUM_PLATFORMS


# ---------------------------------------------------------------------------
# 1. Web target defaults to SeleniumExecutor
# ---------------------------------------------------------------------------

class TestWebTargetDefaults:
    """Verify that web targets work exactly as before."""

    def test_default_target_type_is_web(self):
        """When no target_type is set, default is 'web'."""
        # AITestRunner defaults to web
        runner = AITestRunner(
            suite_path="testsuites/trackpilot.testsuite.md",
            base_url="https://example.com",
        )
        assert runner._target_type == "web"

    def test_web_target_has_selenium_config(self):
        """Web target must have selenium URL and base URL."""
        runner = AITestRunner(
            suite_path="testsuites/trackpilot.testsuite.md",
            base_url="https://example.com",
            selenium_url="http://selenium:4444",
        )
        assert runner._selenium_url == "http://selenium:4444"
        assert runner._base_url == "https://example.com"

    def test_web_target_ignores_native_build_environment(self, monkeypatch):
        monkeypatch.setenv("APP_BUILD_PATH", "/path/to/mobile.apk")
        runner = AITestRunner(
            suite_path="testsuites/trackpilot.testsuite.md",
            base_url="https://example.com",
            target_type="web",
        )
        assert runner._app_build_path == ""

    def test_web_target_requires_base_url_explicit_empty(self):
        """Web target raises ValueError if BASE_URL is explicitly empty and env has no fallback."""
        old_val = os.environ.pop("BASE_URL", None)
        try:
            with pytest.raises(ValueError, match="BASE_URL is required for web targets"):
                AITestRunner(
                    suite_path="testsuites/trackpilot.testsuite.md",
                    base_url="",
                    target_type="web",
                )
        finally:
            if old_val is not None:
                os.environ["BASE_URL"] = old_val


# ---------------------------------------------------------------------------
# 2 & 3. Desktop/Mobile targets select Appium execution
# ---------------------------------------------------------------------------

class TestAppiumTargetSelection:
    """Verify that desktop/mobile targets configure Appium."""

    def test_desktop_target_type(self):
        """Desktop target stores correct target_type."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="desktop",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="macos",
            app_path="/Applications/MyApp.app",
        )
        assert runner._target_type == "desktop"
        assert runner._appium_platform == "macos"
        assert runner._appium_server_url == "http://127.0.0.1:4723"

    def test_mobile_android_target(self):
        """Android mobile target stores correct config."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="mobile",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="android",
            app_path="/path/to/app.apk",
            device_name="emulator-5554",
            app_package="com.example.app",
            app_activity="com.example.app.MainActivity",
        )
        assert runner._target_type == "mobile"
        assert runner._appium_platform == "android"
        assert runner._app_package == "com.example.app"
        assert runner._app_activity == "com.example.app.MainActivity"

    def test_mobile_ios_target(self):
        """iOS mobile target stores correct config."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="mobile",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="ios",
            device_name="iPhone 15",
            bundle_id="com.example.app",
        )
        assert runner._target_type == "mobile"
        assert runner._appium_platform == "ios"
        assert runner._bundle_id == "com.example.app"

    def test_desktop_does_not_require_url(self):
        """Desktop target does not require BASE_URL."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="desktop",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="macos",
            app_path="/Applications/MyApp.app",
        )
        # base_url can be empty for desktop
        assert runner._base_url == "" or runner._base_url is not None

    def test_mobile_does_not_require_url(self):
        """Mobile target does not require BASE_URL."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="mobile",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="android",
            app_build_path="/path/to/app.apk",
            device_name="emulator-5554",
        )
        assert runner._base_url == ""


# ---------------------------------------------------------------------------
# 4 & 5. Missing configuration gives meaningful errors
# ---------------------------------------------------------------------------

class TestConfigurationValidation:
    """Verify that missing configuration produces clear errors."""

    def test_invalid_target_type_raises(self):
        """Invalid target type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid TARGET_TYPE"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="invalid",
            )

    def test_missing_appium_platform_raises(self):
        """Desktop/mobile without APPIUM_PLATFORM raises ValueError."""
        with pytest.raises(ValueError, match="APPIUM_PLATFORM is required"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="desktop",
                appium_server_url="http://127.0.0.1:4723",
                appium_platform="",
                app_build_path="/Applications/MyApp.app",
            )

    def test_invalid_appium_platform_raises(self):
        """Invalid APPIUM_PLATFORM raises ValueError."""
        with pytest.raises(ValueError, match="Invalid APPIUM_PLATFORM"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="desktop",
                appium_server_url="http://127.0.0.1:4723",
                appium_platform="blackberry",
                app_build_path="/Applications/MyApp.app",
            )

    def test_mobile_rejects_desktop_platform(self):
        with pytest.raises(ValueError, match="not valid for TARGET_TYPE=mobile"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="mobile",
                appium_platform="macos",
                app_build_path="/Applications/MyApp.app",
            )

    def test_desktop_rejects_mobile_platform(self):
        with pytest.raises(ValueError, match="not valid for TARGET_TYPE=desktop"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="desktop",
                appium_platform="android",
                app_build_path="/path/to/app.apk",
            )

    def test_missing_appium_server_url_uses_default(self):
        """Desktop without explicit APPIUM_SERVER_URL uses the default."""
        old_val = os.environ.pop("APPIUM_SERVER_URL", None)
        try:
            runner = AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="desktop",
                appium_server_url="",
                appium_platform="macos",
                app_build_path="/Applications/MyApp.app",
            )
            # Falls back to default: http://127.0.0.1:4723
            assert runner._appium_server_url == "http://127.0.0.1:4723"
        finally:
            if old_val is not None:
                os.environ["APPIUM_SERVER_URL"] = old_val

    def test_mobile_requires_app_identity(self, monkeypatch):
        """Mobile target without build path, package, or bundle ID raises ValueError."""
        for name in ("APP_BUILD_PATH", "APP_PATH", "APP_PACKAGE", "BUNDLE_ID"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ValueError, match="APP_BUILD_PATH, BUNDLE_ID, or APP_PACKAGE is required"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="mobile",
                appium_server_url="http://127.0.0.1:4723",
                appium_platform="android",
            )

    def test_desktop_requires_app_identity(self, monkeypatch):
        """Desktop target without build path, bundle ID, or package raises ValueError."""
        for name in ("APP_BUILD_PATH", "APP_PATH", "APP_PACKAGE", "BUNDLE_ID"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ValueError, match="APP_BUILD_PATH, BUNDLE_ID, or APP_PACKAGE is required"):
            AITestRunner(
                suite_path="testsuites/test.testsuite.md",
                target_type="desktop",
                appium_server_url="http://127.0.0.1:4723",
                appium_platform="macos",
            )

    def test_app_build_path_and_app_path_interchangeable(self):
        """Both app_build_path and app_path work and set both attributes."""
        runner1 = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="desktop",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="macos",
            app_build_path="/Applications/MyApp.app",
        )
        assert runner1._app_build_path == "/Applications/MyApp.app"
        assert runner1._app_path == "/Applications/MyApp.app"

        runner2 = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="desktop",
            appium_server_url="http://127.0.0.1:4723",
            appium_platform="macos",
            app_path="/Applications/LegacyApp.app",
        )
        assert runner2._app_build_path == "/Applications/LegacyApp.app"
        assert runner2._app_path == "/Applications/LegacyApp.app"

    def test_app_repo_url_is_optional(self):
        """APP_REPO_URL is optional and stored on the runner."""
        runner = AITestRunner(
            suite_path="testsuites/test.testsuite.md",
            target_type="web",
            base_url="https://example.com",
            app_repo_url="https://github.com/example/repo.git",
        )
        assert runner._app_repo_url == "https://github.com/example/repo.git"


# ---------------------------------------------------------------------------
# 6. Web-only actions are rejected on native apps
# ---------------------------------------------------------------------------

class TestWebOnlyActionRejection:
    """Verify web-only actions don't execute on native apps."""

    def test_web_only_actions_are_defined(self):
        """WEB_ONLY_ACTIONS set is non-empty and contains expected types."""
        assert ActionType.NAVIGATE in WEB_ONLY_ACTIONS
        assert ActionType.VERIFY_URL in WEB_ONLY_ACTIONS
        assert ActionType.EXECUTE_JS in WEB_ONLY_ACTIONS
        assert ActionType.RELOAD_PAGE in WEB_ONLY_ACTIONS
        assert ActionType.GET_CONSOLE_ERRORS in WEB_ONLY_ACTIONS

    def test_native_only_actions_are_defined(self):
        """NATIVE_ONLY_ACTIONS set is non-empty and contains expected types."""
        assert ActionType.TAP in NATIVE_ONLY_ACTIONS
        assert ActionType.SWIPE in NATIVE_ONLY_ACTIONS
        assert ActionType.SCROLL in NATIVE_ONLY_ACTIONS
        assert ActionType.LAUNCH_APP in NATIVE_ONLY_ACTIONS
        assert ActionType.HIDE_KEYBOARD in NATIVE_ONLY_ACTIONS

    def test_web_and_native_actions_are_disjoint(self):
        """Web-only and native-only action sets don't overlap."""
        overlap = WEB_ONLY_ACTIONS & NATIVE_ONLY_ACTIONS
        assert len(overlap) == 0, f"Overlapping actions: {overlap}"

    def test_common_actions_in_neither_set(self):
        """Common actions (CLICK, TYPE, etc.) are not in either exclusive set."""
        common = [ActionType.CLICK, ActionType.TYPE, ActionType.CLEAR,
                  ActionType.VERIFY_TEXT, ActionType.VERIFY_VISIBLE,
                  ActionType.CAPTURE_SCREENSHOT, ActionType.SLEEP]
        for action in common:
            assert action not in WEB_ONLY_ACTIONS
            assert action not in NATIVE_ONLY_ACTIONS


# ---------------------------------------------------------------------------
# 7. Existing Action/TestCase serialization remains compatible
# ---------------------------------------------------------------------------

class TestSerializationCompatibility:
    """Verify that serialization works for both old and new action types."""

    def test_existing_action_serialization(self):
        """Existing web actions serialize correctly."""
        action = Action(
            action_type=ActionType.NAVIGATE,
            description="Open login page",
            value="https://example.com/login",
        )
        d = action.to_dict()
        assert d["action"] == "NAVIGATE"
        assert d["value"] == "https://example.com/login"

    def test_new_native_action_serialization(self):
        """New native actions serialize correctly."""
        action = Action(
            action_type=ActionType.TAP,
            description="Tap login button",
            locator=Locator(LocatorStrategy.ACCESSIBILITY_ID, "login_btn"),
        )
        d = action.to_dict()
        assert d["action"] == "TAP"
        assert d["locator"]["strategy"] == "accessibility_id"
        assert d["locator"]["value"] == "login_btn"

    def test_appium_locator_strategies_serialize(self):
        """Appium-specific locator strategies serialize correctly."""
        strategies = {
            LocatorStrategy.ACCESSIBILITY_ID: "accessibility_id",
            LocatorStrategy.CLASS_NAME: "class_name",
            LocatorStrategy.ANDROID_UIAUTOMATOR: "android_uiautomator",
            LocatorStrategy.IOS_PREDICATE: "ios_predicate",
        }
        for strategy, expected_value in strategies.items():
            loc = Locator(strategy=strategy, value="test_value")
            assert loc.strategy.value == expected_value

    def test_existing_locator_strategies_unchanged(self):
        """Existing web locator strategies still work."""
        strategies = {
            LocatorStrategy.DATA_TESTID: "data-testid",
            LocatorStrategy.ID: "id",
            LocatorStrategy.CSS: "css",
            LocatorStrategy.XPATH: "xpath",
        }
        for strategy, expected_value in strategies.items():
            loc = Locator(strategy=strategy, value="test")
            assert loc.strategy.value == expected_value

    def test_to_selenium_locator_still_works(self):
        """to_selenium_locator works for existing strategies."""
        loc = Locator(LocatorStrategy.ID, "email")
        assert loc.to_selenium_locator() == "id:email"

        loc = Locator(LocatorStrategy.CSS, ".btn")
        assert loc.to_selenium_locator() == "css:.btn"

    def test_to_selenium_locator_handles_appium_strategies(self):
        """to_selenium_locator doesn't crash on Appium strategies."""
        loc = Locator(LocatorStrategy.ACCESSIBILITY_ID, "login_btn")
        result = loc.to_selenium_locator()
        assert "login_btn" in result  # Should return fallback

    def test_test_case_serialization_unchanged(self):
        """TestCase serialization works with new action types."""
        tc = TestCase(
            test_id="TC-TEST-001",
            page="Home",
            functionality="Launch",
            scenario="App launches successfully",
            steps=[
                Action(ActionType.LAUNCH_APP, "Launch the app"),
                Action(ActionType.VERIFY_VISIBLE, "Verify home screen",
                       locator=Locator(LocatorStrategy.ACCESSIBILITY_ID, "home_screen")),
            ],
            expected_result="Home screen is visible",
            test_type="mobile",
        )
        d = tc.to_dict()
        assert d["test_id"] == "TC-TEST-001"
        assert d["test_type"] == "mobile"
        assert len(d["steps"]) == 2
        assert d["steps"][0]["action"] == "LAUNCH_APP"

    def test_test_result_serialization_unchanged(self):
        """TestResult serialization unchanged for Appium results."""
        result = TestResult(
            test_id="TC-TEST-001",
            test_name="App launches",
            status=TestStatus.PASS,
            expected="Home screen visible",
            actual="Test passed",
            duration_seconds=2.5,
            page="Home",
            functionality="Launch",
            url="",  # empty for native apps
            browser="android",
            project="myapp",
        )
        d = result.to_dict()
        assert d["status"] == "PASS"
        assert d["url"] == ""
        assert d["browser"] == "android"


# ---------------------------------------------------------------------------
# 8. TestGenerator respects target type
# ---------------------------------------------------------------------------

class TestGeneratorTargetType:
    """Verify TestGenerator adapts to target type."""

    def test_web_generator_uses_base_url(self):
        """Web generator stores base_url."""
        gen = TestGenerator(
            base_url="https://example.com",
            target_type="web",
        )
        assert gen._base_url == "https://example.com"
        assert gen._target_type == "web"

    def test_desktop_generator_no_url_required(self):
        """Desktop generator does not require base_url."""
        gen = TestGenerator(
            target_type="desktop",
            appium_platform="macos",
        )
        assert gen._target_type == "desktop"
        assert gen._appium_platform == "macos"

    def test_mobile_generator_no_url_required(self):
        """Mobile generator does not require base_url."""
        gen = TestGenerator(
            target_type="mobile",
            appium_platform="android",
        )
        assert gen._target_type == "mobile"

    def test_default_target_type_is_web(self):
        """Default generator target is web."""
        gen = TestGenerator(base_url="https://example.com")
        assert gen._target_type == "web"

    def test_mobile_credential_placeholders_remain_symbolic(self):
        gen = TestGenerator(
            target_type="mobile",
            appium_platform="android",
            email="user@example.com",
            password="secret-value",
        )
        assert gen._resolve_instruction_value("${VALID_USER_EMAIL}") == "{{VALID_USER_EMAIL}}"
        assert gen._resolve_instruction_value("${VALID_USER_PASSWORD}") == "{{VALID_USER_PASSWORD}}"

    def test_desktop_fallback_does_not_generate_android_locator(self):
        gen = TestGenerator(target_type="desktop", appium_platform="macos")
        actions = gen._instruction_to_desktop_actions('Click "Save" button')
        assert actions[0].locator.strategy == LocatorStrategy.ACCESSIBILITY_ID
        assert "android" not in actions[0].locator.value.lower()

    def test_every_markdown_instruction_gets_a_test_case(self):
        generator = TestGenerator(base_url="https://example.com", target_type="web")
        generator._client = None
        page = PageContext(page_name="Users", page_number=1, url_pattern="/users")
        category = TestCategory(
            category_id="1.1",
            category_name="Components",
            instructions=[
                TestInstruction("Verify the Users heading is visible"),
                TestInstruction("Verify the Search input is visible"),
                TestInstruction("Verify the Add User button is visible"),
            ],
        )

        cases = generator._generate_category_tests(page, category)

        assert len(cases) == 3
        assert [case.test_data["source_instruction_index"] for case in cases] == ["1", "2", "3"]
        assert all("md-coverage" in case.tags for case in cases)
        assert len({case.test_id for case in cases}) == 3

    def test_strict_markdown_coverage_rejects_short_plan(self):
        generator = TestGenerator(base_url="https://example.com", target_type="web")
        generator._client = None
        generator._require_full_coverage = True
        page = PageContext(page_name="Users", page_number=1, url_pattern="/users")
        category = TestCategory(
            category_id="1.1",
            category_name="Components",
            instructions=[TestInstruction("First"), TestInstruction("Second")],
        )
        one_case = TestCase(
            test_id="temporary",
            page="Users",
            functionality="Components",
            scenario="First",
            steps=[Action(ActionType.VERIFY_TEXT, "Verify", value="First")],
        )

        with pytest.raises(RuntimeError, match="Full Markdown coverage required"):
            generator._ensure_category_coverage(page, category, [one_case])

    def test_markdown_coverage_discards_extra_generated_cases(self):
        generator = TestGenerator(base_url="https://example.com", target_type="web")
        generator._client = None
        page = PageContext(page_name="Users", page_number=1, url_pattern="/users")
        category = TestCategory(
            category_id="1.1",
            category_name="Components",
            instructions=[TestInstruction("Verify users")],
        )
        cases = [
            TestCase(
                test_id=f"temporary-{index}",
                page="Users",
                functionality="Components",
                scenario=f"Case {index}",
                steps=[Action(ActionType.VERIFY_TEXT, "Verify", value="Users")],
            )
            for index in range(2)
        ]

        covered = generator._ensure_category_coverage(page, category, cases)

        assert len(covered) == 1
        assert covered[0].test_data["source_instruction"] == "Verify users"

    def test_strict_coverage_rejects_demo_limits(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_FULL_MD_COVERAGE", "true")
        monkeypatch.setenv("AI_MAX_TESTS", "1")
        with pytest.raises(ValueError, match="cannot be combined"):
            TestGenerator(base_url="https://example.com", target_type="web")

    def test_strict_coverage_requires_ai_generator(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_FULL_MD_COVERAGE", "true")
        monkeypatch.delenv("AI_MAX_TESTS", raising=False)
        monkeypatch.delenv("AI_MAX_CATEGORIES", raising=False)
        generator = TestGenerator(base_url="https://example.com", target_type="web")
        generator._client = None
        page = PageContext(page_name="Users", page_number=1, url_pattern="/users")
        category = TestCategory(
            category_id="1.1",
            category_name="Components",
            instructions=[TestInstruction("Verify users")],
        )
        with pytest.raises(RuntimeError, match="requires GEMINI_API_KEY"):
            generator._generate_category_tests(page, category)


class TestLiveMetrics:
    def test_result_is_flushed_for_live_grafana(self, tmp_path):
        class FakePublisher:
            def __init__(self):
                self.test_calls = []
                self.suite_calls = []
                self.flush_count = 0

            def publish_test_result(self, **kwargs):
                self.test_calls.append(kwargs)

            def publish_suite_result(self, **kwargs):
                self.suite_calls.append(kwargs)

            def flush(self):
                self.flush_count += 1

        runner = AITestRunner(
            suite_path="testsuites/trackpilot.testsuite.md",
            base_url="https://example.com",
            project_name="trackpilot",
            artifacts_dir=str(tmp_path),
            target_type="web",
        )
        publisher = FakePublisher()
        runner._publisher = publisher
        result = TestResult(
            test_id="TC-WEB-001",
            test_name="Dashboard loads",
            status=TestStatus.PASS,
            duration_seconds=1.5,
            page="Dashboard",
            functionality="Page Load",
        )

        runner._publish_live_progress(result, [result], 1.5)

        assert publisher.flush_count == 1
        assert publisher.suite_calls[0]["total"] == 1
        assert publisher.suite_calls[0]["passed"] == 1


class TestFullRunExecution:
    def test_all_generated_web_cases_run_even_after_a_failure(self, tmp_path, monkeypatch):
        runner = AITestRunner(
            suite_path="testsuites/trackpilot.testsuite.md",
            base_url="https://example.com",
            project_name="trackpilot",
            artifacts_dir=str(tmp_path),
            target_type="web",
        )
        cases = [
            TestCase(
                test_id=f"TC-WEB-{index:03d}",
                page="Admin",
                functionality="Components",
                scenario=f"Case {index}",
                steps=[Action(ActionType.VERIFY_TEXT, "Verify", value="Admin")],
            )
            for index in range(3)
        ]
        calls = []

        def execute(case, shared_driver=None):
            calls.append(case.test_id)
            status = TestStatus.FAIL if len(calls) == 1 else TestStatus.PASS
            return TestResult(
                test_id=case.test_id,
                test_name=case.test_name,
                status=status,
                page=case.page,
                functionality=case.functionality,
            )

        monkeypatch.setattr(runner, "_execute_single_test", execute)
        monkeypatch.setattr(runner, "_publish_live_progress", lambda **kwargs: None)

        results = runner._execute_tests(cases)

        assert calls == [case.test_id for case in cases]
        assert len(results) == len(cases)
        assert results[0].status == TestStatus.FAIL
        assert results[-1].status == TestStatus.PASS


# ---------------------------------------------------------------------------
# 9. Constant validity checks
# ---------------------------------------------------------------------------

class TestConstants:
    """Verify target type and platform constants."""

    def test_valid_target_types(self):
        assert "web" in VALID_TARGET_TYPES
        assert "desktop" in VALID_TARGET_TYPES
        assert "mobile" in VALID_TARGET_TYPES

    def test_valid_appium_platforms(self):
        assert "android" in VALID_APPIUM_PLATFORMS
        assert "ios" in VALID_APPIUM_PLATFORMS
        assert "macos" in VALID_APPIUM_PLATFORMS
        assert "windows" in VALID_APPIUM_PLATFORMS

    def test_wait_url_not_contains_is_a_web_action(self):
        assert ActionType.WAIT_URL_NOT_CONTAINS in WEB_ONLY_ACTIONS

    def test_password_action_value_is_redacted_from_artifacts(self):
        action = Action(
            action_type=ActionType.TYPE,
            description="Enter password",
            locator=Locator(LocatorStrategy.ID, "password"),
            value="do-not-write-this",
        )
        assert action.to_dict()["value"] == "<redacted>"

    def test_ai_email_locator_gets_stable_fallbacks(self):
        generator = TestGenerator(base_url="https://example.com", target_type="web")
        fallbacks = generator._ai_fallback_locators(
            {"description": "Wait for email field"},
            Locator(LocatorStrategy.NAME, "email"),
        )
        assert Locator(LocatorStrategy.ID, "email") in fallbacks


# ---------------------------------------------------------------------------
# 10. Repository Codebase Reader
# ---------------------------------------------------------------------------

class TestRepoReader:
    """Verify RepoReader extracts routes, components, testIDs, and labels."""

    def test_repo_reader_empty_url_returns_none(self):
        from automation.ai.repo_reader import RepoReader
        reader = RepoReader(repo_url="")
        assert reader.fetch_or_locate_repo() is None
        ctx = reader.inspect_codebase()
        assert ctx["repo_path"] is None
        assert ctx["test_ids"] == []

    def test_repo_reader_local_dir(self, tmp_path):
        from automation.ai.repo_reader import RepoReader
        reader = RepoReader(repo_url=str(tmp_path))
        located = reader.fetch_or_locate_repo()
        assert located == tmp_path

    def test_repo_reader_extracts_elements(self, tmp_path):
        from automation.ai.repo_reader import RepoReader
        sample_code = """
        import React from 'react';
        import { View, Text, TextInput, TouchableOpacity } from 'react-native';

        export const LoginScreen = () => {
            return (
                <View>
                    <Route path="/home" />
                    <Screen name="DashboardScreen" />
                    <TextInput testID="email-input" placeholder="Enter your email" />
                    <TouchableOpacity accessibilityLabel="Sign In Button">
                        <Text>Sign In</Text>
                    </TouchableOpacity>
                </View>
            );
        };
        """
        code_file = tmp_path / "LoginScreen.tsx"
        code_file.write_text(sample_code, encoding="utf-8")

        reader = RepoReader(repo_url=str(tmp_path))
        ctx = reader.inspect_codebase(tmp_path, target_type="mobile")

        assert "email-input" in ctx["test_ids"]
        assert "Sign In Button" in ctx["accessibility_labels"]
        assert "Enter your email" in ctx["placeholders"]
        assert "/home" in ctx["routes"]
        assert "DashboardScreen" in ctx["screens"]
        assert "email-input" in ctx["summary_prompt"]

    def test_test_generator_accepts_codebase_context(self):
        from automation.ai.test_generator import TestGenerator
        codebase_ctx = {
            "summary_prompt": "- Test IDs: email-input, signin-btn",
            "repo_url": "https://github.com/example/app.git",
        }
        gen = TestGenerator(
            target_type="mobile",
            appium_platform="android",
            codebase_context=codebase_ctx,
        )
        assert gen._codebase_context == codebase_ctx

    def test_github_token_is_passed_out_of_band(self, monkeypatch):
        from automation.ai.repo_reader import RepoReader
        monkeypatch.setenv("GH_TOKEN", "top-secret-token")
        reader = RepoReader(repo_url="https://github.com/example/private.git")
        git_env = reader._git_env()
        assert "top-secret-token" not in reader.repo_url
        assert git_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert "top-secret-token" not in git_env["GIT_CONFIG_VALUE_0"]


class TestBuildInspector:
    def test_apk_metadata_is_extracted(self, tmp_path):
        from automation.ai.build_inspector import BuildInspector
        apk = tmp_path / "app.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"manifest")
            archive.writestr("lib/arm64-v8a/libapp.so", b"binary")
            archive.writestr(
                "assets/index.android.bundle",
                b'accessibilityLabel:"Sign In",testID:"email-input"',
            )

        context = BuildInspector(str(apk)).inspect()
        assert context["exists"] is True
        assert context["artifact_type"] == "apk"
        assert context["abis"] == ["arm64-v8a"]
        assert len(context["sha256"]) == 64
        assert "email-input" in context["ui_hints"]

    def test_missing_build_has_actionable_error(self, tmp_path):
        from automation.ai.build_inspector import BuildInspector
        context = BuildInspector(str(tmp_path / "missing.apk")).inspect()
        assert context["exists"] is False
        assert "does not exist" in context["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
