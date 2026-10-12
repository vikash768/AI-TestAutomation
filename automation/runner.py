"""
AI Test Runner — Main Orchestrator.

This is the entry point for AI-driven test execution. It:

1. Reads a project-specific testsuite.md
2. Parses it into structured context
3. Generates concrete test cases with action steps
4. Executes each test via Selenium
5. Validates expected vs actual results
6. Captures failure evidence
7. Produces structured logs and metrics
8. Publishes to Prometheus for Grafana visualization

Usage:
    python -m automation.runner --suite testsuites/trackpilot.testsuite.md

    Or via environment variable:
    TEST_SUITE=testsuites/trackpilot.testsuite.md python -m automation.runner
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
from automation.validation.evidence import EvidenceCollector

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("ai_runner")


class AITestRunner:
    """Orchestrates AI-driven test execution end-to-end."""

    def __init__(
        self,
        suite_path: str,
        base_url: str = "",
        browser_name: str = "chrome",
        selenium_url: str = "",
        project_name: str = "",
        artifacts_dir: str = "artifacts",
    ):
        self._suite_path = suite_path
        self._base_url = base_url or os.environ.get("BASE_URL", "https://trackpilot.pods365.com")
        self._browser_name = browser_name or os.environ.get("BROWSER", "chrome")
        self._selenium_url = selenium_url or os.environ.get("SELENIUM_REMOTE_URL", "http://selenium:4444")
        self._project_name = project_name or os.environ.get("PROJECT_NAME", "trackpilot")
        self._artifacts_dir = Path(artifacts_dir)

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
        self._executor: Optional[SeleniumExecutor] = None
        self._evidence: Optional[EvidenceCollector] = None

    def _create_publisher(self) -> MetricsPublisher:
        """Create the appropriate metrics publisher."""
        pushgateway_url = os.environ.get("PUSHGATEWAY_URL", "")
        if pushgateway_url:
            try:
                return PrometheusPushgatewayPublisher(pushgateway_url=pushgateway_url)
            except Exception:
                logger.exception("Failed to create PushgatewayPublisher")
        return NoOpPublisher()

    def _create_driver(self) -> webdriver.Remote:
        """Create a remote Selenium WebDriver."""
        logger.info("Connecting to Selenium at %s", self._selenium_url)
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

    def run(self) -> dict:
        """Execute the full AI-driven test pipeline.

        Returns:
            A summary dict with execution results.
        """
        run_start = time.time()
        logger.info("=" * 60)
        logger.info("AI Test Runner — Starting")
        logger.info("Suite: %s", self._suite_path)
        logger.info("Project: %s", self._project_name)
        logger.info("Browser: %s", self._browser_name)
        logger.info("Run ID: %s", self._run_id)
        logger.info("=" * 60)

        # Step 1: Parse the test suite
        logger.info("[1/6] Parsing test suite...")
        parser = SuiteParser()
        ctx = parser.parse(self._suite_path)
        logger.info("Parsed: %s — %d pages", ctx.application_name, len(ctx.pages))

        # Step 2: Generate test cases
        logger.info("[2/6] Generating test cases...")
        email = os.environ.get("VALID_USER_EMAIL", "")
        password = os.environ.get("VALID_USER_PASSWORD", "")
        generator = TestGenerator(
            base_url=self._base_url,
            email=email,
            password=password,
        )
        test_cases = generator.generate(ctx)
        logger.info("Generated %d test cases", len(test_cases))

        # Save generated test plan
        self._save_test_plan(test_cases)

        # Step 3: Execute tests
        logger.info("[3/6] Executing tests...")
        results = self._execute_tests(test_cases)

        # Step 4: Compute summary
        logger.info("[4/6] Computing results...")
        summary = self._compute_summary(results, time.time() - run_start)

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

        for i, test_case in enumerate(test_cases, 1):
            logger.info(
                "[Test %d/%d] %s — %s",
                i, len(test_cases), test_case.test_id, test_case.scenario,
            )

            result = self._execute_single_test(test_case)
            results.append(result)

            logger.info(
                "  → %s (%.2fs)%s",
                result.status.value,
                result.duration_seconds,
                f" — {result.failure_reason}" if result.failure_reason else "",
            )

        return results

    def _execute_single_test(self, test_case: TestCase) -> TestResult:
        """Execute a single test case with fresh browser session."""
        test_start = time.time()
        driver = None

        try:
            # Create fresh browser for each test
            driver = self._create_driver()
            executor = SeleniumExecutor(
                driver=driver,
                screenshot_dir=str(self._run_dir / "screenshots"),
            )
            evidence = EvidenceCollector(
                executor=executor,
                project=self._project_name,
                browser=self._browser_name,
            )

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
            logger.exception("Unexpected error in test %s", test_case.test_id)

            # Try to capture evidence even on unexpected errors
            classification = FailureClassification.AUTOMATION_ERROR
            if "WebDriver" in str(e) or "connection" in str(e).lower():
                classification = FailureClassification.ENVIRONMENT_ERROR

            return TestResult(
                test_id=test_case.test_id,
                test_name=test_case.test_name,
                status=TestStatus.FAIL,
                expected=test_case.expected_result,
                actual=str(e),
                duration_seconds=duration,
                failure_reason=str(e),
                failure_classification=classification,
                error_details=traceback.format_exc(),
                page=test_case.page,
                functionality=test_case.functionality,
                browser=self._browser_name,
                project=self._project_name,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if driver:
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

        return {
            "run_id": self._run_id,
            "suite": self._suite_path,
            "project": self._project_name,
            "browser": self._browser_name,
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
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

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
            log_entries.append(entry)

        log_path = self._run_dir / "execution.log"
        log_path.write_text(
            "\n".join(json.dumps(e) for e in log_entries),
            encoding="utf-8",
        )

        logger.info("Results saved to: %s", self._run_dir)

    def _publish_metrics(self, results: list[TestResult], summary: dict) -> None:
        """Publish metrics to Prometheus via Pushgateway."""
        suite_name = Path(self._suite_path).stem.replace(".testsuite", "")
        test_type = "browser"

        # Publish individual test results
        for r in results:
            status_str = r.status.value
            self._publisher.publish_test_result(
                project=self._project_name,
                suite=suite_name,
                browser=self._browser_name,
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
            browser=self._browser_name,
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
  python -m automation.runner --suite testsuites/trackpilot.testsuite.md
  python -m automation.runner --suite testsuites/ijp.testsuite.md --project ijp
        """,
    )
    parser.add_argument(
        "--suite",
        default=os.environ.get("TEST_SUITE", "testsuites/trackpilot.testsuite.md"),
        help="Path to the .testsuite.md file (default: $TEST_SUITE or trackpilot)",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_NAME", "trackpilot"),
        help="Project name for metrics (default: $PROJECT_NAME or trackpilot)",
    )
    parser.add_argument(
        "--browser",
        default=os.environ.get("BROWSER", "chrome"),
        help="Browser name (default: $BROWSER or chrome)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", ""),
        help="Application base URL (default: $BASE_URL)",
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

    args = parser.parse_args()

    runner = AITestRunner(
        suite_path=args.suite,
        base_url=args.base_url,
        browser_name=args.browser,
        selenium_url=args.selenium_url,
        project_name=args.project,
        artifacts_dir=args.artifacts,
    )

    summary = runner.run()

    # Exit with non-zero if any tests failed
    if summary["failed"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
