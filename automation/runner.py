"""
AI Test Runner — Main Orchestrator.

This is the entry point for AI-driven test execution. It:

1. Reads a project-specific testsuite.md
2. Parses it into structured context
3. Generates concrete test cases with action steps
4. Executes each test via Selenium (web) or Appium (desktop/mobile)
5. Validates expected vs actual results
6. Captures failure evidence
7. Produces structured logs and metrics
8. Publishes to Prometheus for Grafana visualization

Usage:
    python -m automation.runner --suite testsuites/myapp.testsuite.md

    Or via environment variable:
    TEST_SUITE=testsuites/myapp.testsuite.md python -m automation.runner

All project-specific configuration comes from .env / environment variables.
No project names, URLs, or paths are hardcoded in this file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions

from automation.actions.executor import ActionExecutionError, SeleniumExecutor
from automation.actions.schema import (
    FailureClassification,
    TestCase,
    TestResult,
    TestStatus,
)
from automation.ai.suite_parser import SuiteParser
from automation.ai.test_generator import TestGenerator
from automation.metrics.publisher import (
    MetricsPublisher,
    NoOpPublisher,
    PrometheusPushgatewayPublisher,
)
from automation.secrets.resolver import SecretResolutionError, SecretResolver
from automation.secrets.sanitizer import sanitize_data, sanitize_text
from automation.validation.evidence import EvidenceCollector

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ai_runner")

# ---------------------------------------------------------------------------
# Valid target types and platforms
# ---------------------------------------------------------------------------
VALID_TARGET_TYPES = {"web", "desktop", "mobile"}
VALID_MOBILE_PLATFORMS = {"android", "ios"}
VALID_DESKTOP_PLATFORMS = {"macos", "windows"}
VALID_APPIUM_PLATFORMS = VALID_MOBILE_PLATFORMS | VALID_DESKTOP_PLATFORMS


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class SetupContextError(RuntimeError):
    """Raised when required test inputs or inspected context are unavailable."""


class AITestRunner:
    """Orchestrates AI-driven test execution end-to-end.

    All project-specific configuration comes from constructor parameters,
    which are resolved from environment variables / CLI args. No project
    names, URLs, or application paths are hardcoded.
    """

    def __init__(
        self,
        suite_path: str,
        base_url: str = "",
        browser_name: str = "",
        selenium_url: str = "",
        project_name: str = "",
        artifacts_dir: str = "artifacts",
        target_type: str = "",
        appium_server_url: str = "",
        appium_platform: str = "",
        app_build_path: str = "",
        device_name: str = "",
        app_package: str = "",
        app_activity: str = "",
        bundle_id: str = "",
        app_repo_url: str = "",
        app_path: str = "",
    ):
        self._suite_path = suite_path or os.environ.get("TEST_SUITE", "")
        self._project_name = project_name or os.environ.get("PROJECT_NAME", "")
        self._artifacts_dir = Path(artifacts_dir)
        self._app_repo_url = app_repo_url or os.environ.get("APP_REPO_URL", "")
        self._require_repo_context = _env_bool("REQUIRE_REPO_CONTEXT")
        self._require_build_context = _env_bool("REQUIRE_BUILD_CONTEXT")
        self._appium_server_url = ""
        self._appium_platform = ""
        self._app_build_path = ""
        self._device_name = ""
        self._app_package = ""
        self._app_activity = ""
        self._bundle_id = ""

        # Target type (web / desktop / mobile)
        self._target_type = (target_type or os.environ.get("TARGET_TYPE", "web")).lower()
        if self._target_type not in VALID_TARGET_TYPES:
            raise ValueError(
                f"Invalid TARGET_TYPE '{self._target_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_TARGET_TYPES))}"
            )

        # --- Platform-specific configuration ---
        if self._target_type == "web":
            self._base_url = base_url or os.environ.get("BASE_URL", "")
            self._browser_name = browser_name or os.environ.get("BROWSER", "chrome")
            self._selenium_url = selenium_url or os.environ.get("SELENIUM_REMOTE_URL", "http://selenium:4444")

            # Validate: web targets MUST have a BASE_URL
            if not self._base_url:
                raise ValueError(
                    "BASE_URL is required for web targets. "
                    "Set it in .env or pass --base-url."
                )
        else:
            # Desktop / mobile — Appium configuration
            self._base_url = ""  # Not used for native targets
            self._browser_name = ""
            self._selenium_url = ""
            self._appium_server_url = appium_server_url or os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723")
            self._appium_platform = (appium_platform or os.environ.get("APPIUM_PLATFORM", "")).lower()
            self._app_build_path = (
                app_build_path
                or app_path
                or os.environ.get("APP_BUILD_PATH", "")
                or os.environ.get("APP_PATH", "")
            )
            self._device_name = device_name or os.environ.get("DEVICE_NAME", "")
            self._app_package = app_package or os.environ.get("APP_PACKAGE", "")
            self._app_activity = app_activity or os.environ.get("APP_ACTIVITY", "")
            self._bundle_id = bundle_id or os.environ.get("BUNDLE_ID", "")

            # Validate: Appium MUST have a platform
            if not self._appium_server_url:
                raise ValueError(
                    "APPIUM_SERVER_URL is required for desktop/mobile targets. "
                    "Set it in .env or pass --appium-server-url."
                )
            if not self._appium_platform:
                raise ValueError(
                    f"APPIUM_PLATFORM is required for {self._target_type} targets. "
                    f"Must be one of: {', '.join(sorted(VALID_APPIUM_PLATFORMS))}. "
                    f"Set it in .env or pass --appium-platform."
                )
            if self._appium_platform not in VALID_APPIUM_PLATFORMS:
                raise ValueError(
                    f"Invalid APPIUM_PLATFORM '{self._appium_platform}'. "
                    f"Must be one of: {', '.join(sorted(VALID_APPIUM_PLATFORMS))}"
                )
            allowed_platforms = (
                VALID_MOBILE_PLATFORMS
                if self._target_type == "mobile"
                else VALID_DESKTOP_PLATFORMS
            )
            if self._appium_platform not in allowed_platforms:
                raise ValueError(
                    f"APPIUM_PLATFORM '{self._appium_platform}' is not valid for "
                    f"TARGET_TYPE={self._target_type}. Use one of: "
                    f"{', '.join(sorted(allowed_platforms))}"
                )

            # Validate: app identity — need at least one way to identify the app
            has_app_identity = bool(
                self._app_build_path
                or self._bundle_id
                or self._app_package
            )
            if not has_app_identity:
                raise ValueError(
                    f"APP_BUILD_PATH, BUNDLE_ID, or APP_PACKAGE is required for "
                    f"{self._target_type} targets. Provide at least one way to "
                    f"identify the application under test."
                )

        # Run ID for this execution
        self._run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._run_dir = self._artifacts_dir / "runs" / self._run_id

        # Create run directories
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "screenshots").mkdir(exist_ok=True)

        # Metrics publisher
        self._publisher = self._create_publisher()

        # State
        self._driver: Optional[webdriver.Remote] = None
        self._executor = None
        self._evidence: Optional[EvidenceCollector] = None
        self._live_metrics_enabled = True

    @property
    def _app_path(self) -> str:
        """Backward-compatibility alias for _app_build_path."""
        return self._app_build_path

    def _create_publisher(self) -> MetricsPublisher:
        """Create the appropriate metrics publisher."""
        pushgateway_url = os.environ.get("PUSHGATEWAY_URL", "")
        if pushgateway_url:
            import socket
            import urllib.parse
            parsed = urllib.parse.urlparse(pushgateway_url)
            host = parsed.hostname or ""
            # Verify if the host is resolvable; if 'pushgateway' (Docker network name) fails on host, try localhost
            try:
                if host:
                    socket.gethostbyname(host)
                return PrometheusPushgatewayPublisher(pushgateway_url=pushgateway_url)
            except socket.gaierror:
                if host == "pushgateway":
                    try:
                        socket.gethostbyname("localhost")
                        local_url = pushgateway_url.replace("pushgateway", "localhost")
                        return PrometheusPushgatewayPublisher(pushgateway_url=local_url)
                    except Exception:
                        pass
                logger.warning(
                    "Pushgateway at '%s' is not resolvable on this host. Disabling Prometheus push.",
                    sanitize_text(pushgateway_url),
                )
            except Exception:
                logger.warning(
                    "Failed to initialize PushgatewayPublisher for %s",
                    sanitize_text(pushgateway_url),
                )
        return NoOpPublisher()

    def _create_driver(self) -> webdriver.Remote:
        """Create a remote Selenium WebDriver."""
        logger.info("Connecting to Selenium at %s", sanitize_text(self._selenium_url))
        options = ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")

        driver = webdriver.Remote(
            command_executor=self._selenium_url,
            options=options,
        )
        driver.set_window_size(1920, 1080)
        driver.implicitly_wait(5)
        logger.info("WebDriver connected successfully")
        return driver

    def _create_appium_driver(self):
        """Create an Appium WebDriver session with platform-appropriate capabilities."""
        from appium import webdriver as appium_webdriver
        from appium.options.common import AppiumOptions

        logger.info(
            "Connecting to Appium at %s (platform=%s)",
            sanitize_text(self._appium_server_url),
            self._appium_platform,
        )

        options = AppiumOptions()

        if self._appium_platform == "android":
            options.set_capability("platformName", "Android")
            options.set_capability("automationName", "UiAutomator2")
            if self._device_name:
                options.set_capability("deviceName", self._device_name)
            if self._app_package:
                options.set_capability("appPackage", self._app_package)
                options.set_capability("noReset", True)
                options.set_capability("dontStopAppOnReset", True)
                if self._app_activity:
                    options.set_capability("appActivity", self._app_activity)
            elif self._app_build_path:
                options.set_capability("app", self._app_build_path)

        elif self._appium_platform == "ios":
            options.set_capability("platformName", "iOS")
            options.set_capability("automationName", "XCUITest")
            if self._device_name:
                options.set_capability("deviceName", self._device_name)
            if self._app_build_path:
                options.set_capability("app", self._app_build_path)
            if self._bundle_id:
                options.set_capability("bundleId", self._bundle_id)

        elif self._appium_platform == "macos":
            options.set_capability("platformName", "mac")
            options.set_capability("automationName", "mac2")
            if self._app_build_path:
                options.set_capability("app", self._app_build_path)
            if self._bundle_id:
                options.set_capability("bundleId", self._bundle_id)

        elif self._appium_platform == "windows":
            options.set_capability("platformName", "Windows")
            options.set_capability("automationName", "windows")
            if self._app_build_path:
                options.set_capability("app", self._app_build_path)

        driver = appium_webdriver.Remote(
            command_executor=self._appium_server_url,
            options=options,
        )
        driver.implicitly_wait(5)
        logger.info("Appium session created successfully (platform=%s)", self._appium_platform)
        return driver

    def run(self) -> dict:
        """Execute the full AI-driven test pipeline.

        Returns:
            A summary dict with execution results.
        """
        run_start = time.time()
        logger.info("=" * 60)
        logger.info("AI Test Runner — Starting")
        logger.info("Suite: %s", self._suite_path)
        logger.info("Project: %s", self._project_name or "(not set)")
        logger.info("Target: %s", self._target_type)
        if self._target_type == "web":
            logger.info("Browser: %s", self._browser_name)
            logger.info("URL: %s", sanitize_text(self._base_url))
        else:
            logger.info("Platform: %s", self._appium_platform)
            logger.info("App: %s", self._app_build_path or self._bundle_id or self._app_package or "(not set)")
        if self._app_repo_url:
            logger.info("Repo: %s", sanitize_text(self._app_repo_url))
        logger.info("Run ID: %s", self._run_id)
        logger.info("=" * 60)

        # Step 1: Parse the test suite
        logger.info("[1/6] Parsing test suite...")
        parser = SuiteParser()
        ctx = parser.parse(self._suite_path)
        logger.info("Parsed: %s — %d pages", ctx.application_name, len(ctx.pages))

        # Step 1b: Ground generation in repository source and the exact build.
        codebase_context = {}
        inspection_context: dict[str, dict] = {"repository": {}, "build": {}}
        if self._require_repo_context and not self._app_repo_url:
            raise SetupContextError(
                "Repository context is required but APP_REPO_URL is empty. Set the "
                f"{self._target_type.upper()}_APP_REPO_URL variable or APP_REPO_URL."
            )
        if self._app_repo_url:
            logger.info(
                "Reading codebase through repository: %s...",
                sanitize_text(self._app_repo_url),
            )
            try:
                from automation.ai.repo_reader import RepoReader
                reader = RepoReader(
                    repo_url=self._app_repo_url,
                    cache_dir=self._artifacts_dir / "repos",
                )
                codebase_context = reader.inspect_codebase(target_type=self._target_type)
                inspection_context["repository"] = codebase_context
                scanned = codebase_context.get("scanned_files", 0)
                if scanned > 0:
                    logger.info(
                        "Codebase inspection complete: scanned %d files (%d routes, %d testIDs, %d accessibility labels, %d screens)",
                        scanned,
                        len(codebase_context.get("routes", [])),
                        len(codebase_context.get("test_ids", [])),
                        len(codebase_context.get("accessibility_labels", [])),
                        len(codebase_context.get("screens", [])),
                    )
                elif self._require_repo_context:
                    detail = codebase_context.get("repo_error") or "no scannable source files were found"
                    raise SetupContextError(
                        f"Required repository context is unavailable for {self._app_repo_url}: {detail}. "
                        "For a private GitHub repository, correct the URL/access, run 'gh auth login', "
                        "set GH_TOKEN/GITHUB_TOKEN, or use an authenticated local repository path."
                    )
            except Exception as e:
                if self._require_repo_context:
                    if isinstance(e, SetupContextError):
                        raise
                    raise SetupContextError(f"Required repository inspection failed: {e}") from e
                logger.warning(
                    "Codebase reading from '%s' encountered an issue: %s. Proceeding with suite instructions.",
                    self._app_repo_url, e
                )

        build_context = {}
        if self._target_type != "web" and self._app_build_path:
            from automation.ai.build_inspector import BuildInspector
            logger.info("Inspecting application build: %s", self._app_build_path)
            build_context = BuildInspector(self._app_build_path).inspect()
            inspection_context["build"] = build_context
            if build_context.get("error"):
                if self._require_build_context:
                    raise SetupContextError(build_context["error"])
                logger.warning("Build inspection issue: %s", build_context["error"])
            else:
                logger.info(
                    "Build inspection complete: type=%s sha256=%s",
                    build_context.get("artifact_type", "unknown"),
                    str(build_context.get("sha256", "directory-build"))[:12],
                )
        elif self._target_type != "web" and self._require_build_context:
            raise SetupContextError(
                "Build context is required but APP_BUILD_PATH is empty. Set the "
                f"{self._target_type.upper()}_APP_BUILD_PATH variable or APP_BUILD_PATH."
            )

        summaries = []
        if codebase_context.get("summary_prompt"):
            summaries.append("Repository inspection:\n" + codebase_context["summary_prompt"])
        if build_context.get("summary_prompt"):
            summaries.append("Build inspection:\n" + build_context["summary_prompt"])
        if summaries:
            codebase_context["summary_prompt"] = "\n".join(summaries)
            codebase_context["build"] = build_context

        inspection_path = self._run_dir / "inspection-context.json"
        inspection_path.write_text(
            json.dumps(sanitize_data(inspection_context), indent=2),
            encoding="utf-8",
        )
        logger.info("Inspection evidence saved: %s", inspection_path)

        # Step 2: Generate test cases
        logger.info("[2/6] Generating test cases...")
        generator = TestGenerator(
            base_url=self._base_url,
            target_type=self._target_type,
            appium_platform=getattr(self, "_appium_platform", ""),
            codebase_context=codebase_context,
        )
        test_cases = generator.generate(ctx)
        logger.info("Generated %d test cases", len(test_cases))

        expected_instructions = sum(
            len(category.instructions)
            for page in ctx.pages
            for category in page.categories
        )
        covered_instructions = sum(
            1
            for case in test_cases
            if case.test_data.get("source_instruction_index")
        )
        coverage_summary = {
            "suite": self._suite_path,
            "pages": len(ctx.pages),
            "categories": sum(len(page.categories) for page in ctx.pages),
            "markdown_instructions": expected_instructions,
            "covered_instructions": covered_instructions,
            "generated_tests": len(test_cases),
            "full_coverage_required": generator._require_full_coverage,
            "coverage_percent": round(
                (covered_instructions / expected_instructions * 100)
                if expected_instructions else 100.0,
                2,
            ),
        }
        if generator._require_full_coverage and covered_instructions < expected_instructions:
            raise SetupContextError(
                f"Full Markdown coverage failed: {covered_instructions}/{expected_instructions} "
                "instructions have generated tests."
            )
        coverage_path = self._run_dir / "coverage-summary.json"
        coverage_path.write_text(
            json.dumps(sanitize_data(coverage_summary), indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Markdown coverage: %d/%d instructions (%.2f%%)",
            covered_instructions,
            expected_instructions,
            coverage_summary["coverage_percent"],
        )

        # Save generated test plan
        self._save_test_plan(test_cases)

        # Step 3: Execute tests
        logger.info("[3/6] Executing tests...")
        results = self._execute_tests(test_cases)
        if len(results) != len(test_cases):
            raise RuntimeError(
                "Execution integrity failed: "
                f"{len(results)}/{len(test_cases)} generated tests completed."
            )
        logger.info(
            "Execution integrity: %d/%d generated tests completed",
            len(results),
            len(test_cases),
        )

        # Step 4: Compute summary
        logger.info("[4/6] Computing results...")
        summary = self._compute_summary(results, time.time() - run_start)
        summary["planned_tests"] = len(test_cases)
        summary["completed_tests"] = len(results)
        summary["execution_complete"] = len(results) == len(test_cases)

        # Step 5: Save results
        logger.info("[5/6] Saving results...")
        self._save_results(results, summary)

        # Step 6: Publish metrics
        logger.info("[6/6] Publishing metrics...")
        self._publish_metrics(results, summary)

        logger.info("=" * 60)
        logger.info("AI Test Runner — Complete")
        logger.info(
            "Total: %d | Pass: %d | Fail: %d | Skip: %d | Duration: %.1fs",
            summary["total"],
            summary["passed"],
            summary["failed"],
            summary["skipped"],
            summary["duration_seconds"],
        )
        logger.info("Results: %s", self._run_dir)
        logger.info("=" * 60)

        return summary

    def _execute_tests(self, test_cases: list[TestCase]) -> list[TestResult]:
        """Execute all test cases and collect results."""
        results: list[TestResult] = []
        shared_driver = None
        execution_start = time.time()

        # For mobile/desktop and web, reuse the driver session across tests to prevent
        # repeated startup/teardown crashes and speed up execution at one go.
        if self._target_type != "web":
            try:
                shared_driver = self._create_appium_driver()
            except Exception as e:
                logger.warning(
                    "Failed to initialize shared Appium session: %s. Using per-test sessions.",
                    sanitize_text(str(e)),
                )
                shared_driver = None
        else:
            try:
                shared_driver = self._create_driver()
            except Exception as e:
                logger.warning(
                    "Failed to initialize shared Selenium session: %s. Using per-test sessions.",
                    sanitize_text(str(e)),
                )
                shared_driver = None

        try:
            for i, test_case in enumerate(test_cases, 1):
                logger.info(
                    "[Test %d/%d] %s — %s",
                    i, len(test_cases), test_case.test_id, test_case.scenario,
                )

                if self._target_type == "web" and (shared_driver is None or not self._is_driver_alive(shared_driver)):
                    try:
                        shared_driver = self._create_driver()
                    except Exception as exc:
                        logger.warning(
                            "Failed to recreate shared Selenium driver: %s",
                            sanitize_text(str(exc)),
                        )
                        shared_driver = None

                result = self._execute_single_test(test_case, shared_driver=shared_driver)
                results.append(result)

                logger.info(
                    "  → %s (%.2fs)%s",
                    result.status.value,
                    result.duration_seconds,
                    f" — {sanitize_text(result.failure_reason)}" if result.failure_reason else "",
                )
                self._publish_live_progress(
                    result=result,
                    results=results,
                    duration=time.time() - execution_start,
                )
        finally:
            if shared_driver:
                try:
                    shared_driver.quit()
                except Exception:
                    pass

        return results

    def _is_driver_alive(self, driver) -> bool:
        """Check if WebDriver is alive and responsive."""
        if not driver:
            return False
        try:
            _ = driver.current_url
            return True
        except Exception:
            return False

    def _ensure_web_authenticated(self, driver) -> None:
        """Ensure web session is authenticated with credentials for protected admin panel pages."""
        if not self._base_url:
            return

        try:
            resolver = SecretResolver()
            email = resolver.resolve("{{VALID_USER_EMAIL}}")
            password = resolver.resolve("{{VALID_USER_PASSWORD}}")
        except SecretResolutionError:
            return

        try:
            curr = driver.current_url or ""
            authenticated_routes = (
                "dashboard", "users", "live", "history",
                "screenshots", "projects", "reports",
                "settings", "audit", "profile"
            )
            if any(f"/{r}" in curr for r in authenticated_routes):
                return

            logger.info("Auto-authenticating TrackPilot session...")
            driver.get(f"{self._base_url}/login")
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 8.0).until(
                EC.visibility_of_element_located((By.ID, "email"))
            )
            email_field = driver.find_element(By.ID, "email")
            email_field.clear()
            email_field.send_keys(email)
            pass_field = driver.find_element(By.ID, "password")
            pass_field.clear()
            pass_field.send_keys(password)
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            WebDriverWait(driver, 10.0).until(lambda d: "/login" not in d.current_url)
            logger.info("Session authenticated successfully (current URL: %s)", driver.current_url)
        except Exception as exc:
            logger.debug(
                "Auto-authentication check completed/bypassed: %s",
                sanitize_text(str(exc)),
            )

    def _publish_live_progress(
        self,
        result: TestResult,
        results: list[TestResult],
        duration: float,
    ) -> None:
        """Push cumulative metrics after each test so Grafana updates live."""
        if not self._live_metrics_enabled:
            return

        suite_name = Path(self._suite_path).stem.replace(".testsuite", "")
        platform_label = (
            self._browser_name
            if self._target_type == "web"
            else getattr(self, "_appium_platform", "")
        )
        passed = sum(1 for item in results if item.status == TestStatus.PASS)
        failed = sum(1 for item in results if item.status == TestStatus.FAIL)
        skipped = sum(1 for item in results if item.status == TestStatus.SKIPPED)

        try:
            self._publisher.publish_test_result(
                project=self._project_name,
                suite=suite_name,
                browser=platform_label,
                test_type=self._target_type,
                test_id=result.test_id,
                test_name=result.test_name,
                page=result.page,
                functionality=result.functionality,
                run_id=self._run_id,
                status=result.status.value,
                duration_seconds=result.duration_seconds,
            )
            self._publisher.publish_suite_result(
                project=self._project_name,
                suite=suite_name,
                browser=platform_label,
                test_type=self._target_type,
                total=len(results),
                passed=passed,
                failed=failed,
                skipped=skipped,
                duration_seconds=duration,
            )
            self._publisher.flush()
            logger.info(
                "Live Grafana metrics updated: %d completed (%d pass, %d fail, %d skip)",
                len(results),
                passed,
                failed,
                skipped,
            )
        except Exception as exc:
            self._live_metrics_enabled = False
            logger.warning(
                "Live metrics update failed; tests will continue and final publishing will be retried: %s",
                exc,
            )

    def _execute_single_test(self, test_case: TestCase, shared_driver=None) -> TestResult:
        """Execute a single test case."""
        test_start = time.time()
        driver = shared_driver
        owns_driver = False

        try:
            if self._target_type == "web":
                if driver is None:
                    driver = self._create_driver()
                    owns_driver = True

                # For protected pages on web (non-login), ensure the session is authenticated
                if test_case.page.lower() != "login":
                    self._ensure_web_authenticated(driver)

                executor = SeleniumExecutor(
                    driver=driver,
                    screenshot_dir=str(self._run_dir / "screenshots"),
                )
            else:
                from automation.actions.appium_executor import AppiumExecutor
                if driver is None:
                    driver = self._create_appium_driver()
                    owns_driver = True
                executor = AppiumExecutor(
                    driver=driver,
                    screenshot_dir=str(self._run_dir / "screenshots"),
                    platform=self._appium_platform,
                )

            # Determine browser/platform label for evidence
            platform_label = self._browser_name if self._target_type == "web" else self._appium_platform

            evidence = EvidenceCollector(
                executor=executor,
                project=self._project_name,
                browser=platform_label,
            )

            # For mobile, ensure the target application is in foreground
            if self._target_type != "web" and self._app_package:
                try:
                    curr_pkg = getattr(driver, "current_package", None)
                    if curr_pkg and curr_pkg != self._app_package and ("launcher" in curr_pkg.lower() or curr_pkg in ("com.google.android.apps.nexuslauncher", "com.android.launcher3", "com.android.systemui")):
                        logger.info("Target package %s not in foreground (current: %s). Activating...", self._app_package, curr_pkg)
                        driver.activate_app(self._app_package)
                        time.sleep(1.0)
                except Exception as e:
                    logger.debug("Foreground check error: %s", e)

            # Execute each action step
            for step_idx, action in enumerate(test_case.steps):
                try:
                    executor.execute(action)
                except ActionExecutionError as e:
                    duration = time.time() - test_start
                    result = evidence.collect_failure_evidence(
                        test_case=test_case,
                        error_message=str(e),
                        classification=e.classification,
                        duration=duration,
                    )
                    return result

            # All steps passed
            duration = time.time() - test_start
            return evidence.create_pass_result(test_case, duration)

        except Exception as e:
            duration = time.time() - test_start
            safe_error = sanitize_text(str(e))
            logger.error("Unexpected error in test %s: %s", test_case.test_id, safe_error)

            # Try to capture evidence even on unexpected errors
            classification = FailureClassification.AUTOMATION_ERROR
            if "WebDriver" in str(e) or "connection" in str(e).lower():
                classification = FailureClassification.ENVIRONMENT_ERROR

            platform_label = self._browser_name if self._target_type == "web" else getattr(self, "_appium_platform", "")

            return TestResult(
                test_id=test_case.test_id,
                test_name=test_case.test_name,
                status=TestStatus.FAIL,
                expected=test_case.expected_result,
                actual=safe_error,
                duration_seconds=duration,
                failure_reason=safe_error,
                failure_classification=classification,
                error_details=sanitize_text(traceback.format_exc()),
                page=test_case.page,
                functionality=test_case.functionality,
                browser=platform_label,
                project=self._project_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if owns_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    def _compute_summary(self, results: list[TestResult], duration: float) -> dict:
        """Compute execution summary statistics."""
        total = len(results)
        passed = sum(1 for r in results if r.status == TestStatus.PASS)
        failed = sum(1 for r in results if r.status == TestStatus.FAIL)
        skipped = sum(1 for r in results if r.status == TestStatus.SKIPPED)
        pass_rate = (passed / total * 100) if total > 0 else 0

        # Failure classification breakdown
        classifications: dict[str, int] = {}
        for r in results:
            if r.failure_classification:
                key = r.failure_classification.value
                classifications[key] = classifications.get(key, 0) + 1

        platform_label = self._browser_name if self._target_type == "web" else getattr(self, "_appium_platform", "")

        return {
            "run_id": self._run_id,
            "suite": self._suite_path,
            "project": self._project_name,
            "target_type": self._target_type,
            "browser": platform_label,
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": round(pass_rate, 2),
            "duration_seconds": round(duration, 2),
            "failure_classifications": classifications,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _save_test_plan(self, test_cases: list[TestCase]) -> None:
        """Save the generated test plan to disk."""
        plan_path = self._run_dir / "test-plan.json"
        plan_data = [tc.to_dict() for tc in test_cases]
        plan_path.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")
        logger.info("Test plan saved: %s (%d cases)", plan_path, len(test_cases))

    def _save_results(self, results: list[TestResult], summary: dict) -> None:
        """Save execution results and summary to disk."""
        # Save individual results
        results_path = self._run_dir / "test-results.json"
        results_data = [r.to_dict() for r in results]
        results_path.write_text(json.dumps(results_data, indent=2), encoding="utf-8")

        # Save summary
        summary_path = self._run_dir / "summary.json"
        summary_path.write_text(json.dumps(sanitize_data(summary), indent=2), encoding="utf-8")

        # Save failures separately for quick inspection
        failures = [r.to_dict() for r in results if r.status == TestStatus.FAIL]
        if failures:
            failures_path = self._run_dir / "failures.json"
            failures_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")

        # Save execution log
        log_entries = []
        for r in results:
            entry = {
                "test_id": r.test_id,
                "status": r.status.value,
                "duration": round(r.duration_seconds, 3),
                "page": r.page,
            }
            if r.failure_reason:
                entry["failure"] = r.failure_reason
            if r.failure_classification:
                entry["classification"] = r.failure_classification.value
            log_entries.append(sanitize_data(entry))

        log_path = self._run_dir / "execution.log"
        log_path.write_text(
            "\n".join(json.dumps(e) for e in log_entries),
            encoding="utf-8",
        )

        logger.info("Results saved to: %s", self._run_dir)

    def _publish_metrics(self, results: list[TestResult], summary: dict) -> None:
        """Publish metrics to Prometheus via Pushgateway."""
        suite_name = Path(self._suite_path).stem.replace(".testsuite", "")
        test_type = self._target_type
        platform_label = self._browser_name if self._target_type == "web" else getattr(self, "_appium_platform", "")

        # Publish individual test results
        for r in results:
            status_str = r.status.value
            self._publisher.publish_test_result(
                project=self._project_name,
                suite=suite_name,
                browser=platform_label,
                test_type=test_type,
                test_id=r.test_id,
                test_name=getattr(r, "test_name", r.test_id),
                page=r.page,
                functionality=getattr(r, "functionality", ""),
                run_id=self._run_id,
                status=status_str,
                duration_seconds=r.duration_seconds,
            )

        # Publish suite-level metrics
        self._publisher.publish_suite_result(
            project=self._project_name,
            suite=suite_name,
            browser=platform_label,
            test_type=test_type,
            total=summary["total"],
            passed=summary["passed"],
            failed=summary["failed"],
            skipped=summary["skipped"],
            duration_seconds=summary["duration_seconds"],
        )

        # Flush metrics
        try:
            self._publisher.flush()
            logger.info("Metrics published to Prometheus")
        except Exception:
            logger.exception("Failed to publish metrics")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AI-Driven Test Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Web:
  python -m automation.runner --suite testsuites/myapp.testsuite.md \\
      --base-url https://myapp.example.com

  # Desktop (macOS app via Appium):
  python -m automation.runner --suite testsuites/myapp-desktop.testsuite.md \\
      --target-type desktop --appium-platform macos \\
      --app-build-path /Applications/MyApp.app

  # Android (mobile via Appium):
  python -m automation.runner --suite testsuites/myapp-mobile.testsuite.md \\
      --target-type mobile --appium-platform android \\
      --app-build-path /path/to/app.apk --device-name emulator-5554
        """,
    )
    parser.add_argument(
        "--suite",
        default=os.environ.get("TEST_SUITE", ""),
        help="Path to the .testsuite.md file (default: $TEST_SUITE)",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_NAME", ""),
        help="Project name for metrics (default: $PROJECT_NAME)",
    )
    parser.add_argument(
        "--browser",
        default=os.environ.get("BROWSER", "chrome"),
        help="Browser name for web targets (default: $BROWSER or chrome)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", ""),
        help="Application base URL for web targets (default: $BASE_URL)",
    )
    parser.add_argument(
        "--selenium-url",
        default=os.environ.get("SELENIUM_REMOTE_URL", "http://selenium:4444"),
        help="Selenium remote URL (default: $SELENIUM_REMOTE_URL)",
    )
    parser.add_argument(
        "--artifacts",
        default="artifacts",
        help="Artifacts directory (default: artifacts)",
    )
    # --- Target type ---
    parser.add_argument(
        "--target-type",
        default=os.environ.get("TARGET_TYPE", "web"),
        choices=["web", "desktop", "mobile"],
        help="Target type: web, desktop, or mobile (default: $TARGET_TYPE or web)",
    )
    # --- Appium configuration ---
    parser.add_argument(
        "--appium-server-url",
        default=os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723"),
        help="Appium server URL (default: $APPIUM_SERVER_URL or http://127.0.0.1:4723)",
    )
    parser.add_argument(
        "--appium-platform",
        default=os.environ.get("APPIUM_PLATFORM", ""),
        help="Appium platform: android, ios, macos, windows (default: $APPIUM_PLATFORM)",
    )
    parser.add_argument(
        "--app-build-path",
        "--app-path",
        dest="app_build_path",
        default=os.environ.get("APP_BUILD_PATH", os.environ.get("APP_PATH", "")),
        help="Path to application build artifact — .apk, .ipa, .app, .exe (default: $APP_BUILD_PATH)",
    )
    parser.add_argument(
        "--device-name",
        default=os.environ.get("DEVICE_NAME", ""),
        help="Device name for mobile testing (default: $DEVICE_NAME)",
    )
    parser.add_argument(
        "--app-package",
        default=os.environ.get("APP_PACKAGE", ""),
        help="Android app package (default: $APP_PACKAGE)",
    )
    parser.add_argument(
        "--app-activity",
        default=os.environ.get("APP_ACTIVITY", ""),
        help="Android app activity (default: $APP_ACTIVITY)",
    )
    parser.add_argument(
        "--bundle-id",
        default=os.environ.get("BUNDLE_ID", ""),
        help="iOS/macOS bundle identifier (default: $BUNDLE_ID)",
    )
    parser.add_argument(
        "--app-repo-url",
        default=os.environ.get("APP_REPO_URL", ""),
        help="Optional source repository URL for AI inspection (default: $APP_REPO_URL)",
    )

    args = parser.parse_args()

    runner = AITestRunner(
        suite_path=args.suite,
        base_url=args.base_url,
        browser_name=args.browser,
        selenium_url=args.selenium_url,
        project_name=args.project,
        artifacts_dir=args.artifacts,
        target_type=args.target_type,
        appium_server_url=args.appium_server_url,
        appium_platform=args.appium_platform,
        app_build_path=args.app_build_path,
        device_name=args.device_name,
        app_package=args.app_package,
        app_activity=args.app_activity,
        bundle_id=args.bundle_id,
        app_repo_url=args.app_repo_url,
    )

    try:
        summary = runner.run()
    except SetupContextError as exc:
        logger.error("Test setup failed: %s", sanitize_text(str(exc)))
        sys.exit(2)

    # Exit with non-zero if any tests failed
    if summary["failed"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
