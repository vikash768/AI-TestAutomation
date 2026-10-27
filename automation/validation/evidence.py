"""
Evidence Collector.

Captures diagnostic evidence on test failures:
- Screenshots (with URL generation for Grafana)
- Browser console errors (web) or empty (native)
- Current URL (web) or empty (native)
- Error details
- Failure classification
- Bug categorization (UI_BUG, FUNCTIONAL_BUG, API_BUG)
- Failed step traceability back to source .testsuite.md

Works with both SeleniumExecutor and AppiumExecutor via duck typing.
Both executors provide: capture_screenshot(), get_console_errors(), get_current_url().
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from automation.executors.schema import (
    BugCategory,
    FailureClassification,
    TestCase,
    TestResult,
    TestStatus,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ExecutorProtocol(Protocol):
    """Protocol that both SeleniumExecutor and AppiumExecutor satisfy."""

    def capture_screenshot(self, name: str = "failure") -> str | None: ...
    def get_console_errors(self) -> list[str]: ...
    def get_current_url(self) -> str: ...


class EvidenceCollector:
    """Collects failure evidence from the executor state."""

    def __init__(self, executor: ExecutorProtocol, project: str = "", browser: str = ""):

        self._executor = executor
        self._project = project
        self._browser = browser
        # Base URL for serving screenshot artifacts in Grafana.
        # When set, screenshot_path is converted to a clickable URL.
        self._artifact_server_url = os.environ.get("ARTIFACT_SERVER_URL", "").rstrip("/")

    def _screenshot_to_url(self, screenshot_path: str | None) -> str:
        """Convert a local screenshot path to a Grafana-accessible URL."""
        if not screenshot_path:
            return ""
        if self._artifact_server_url:
            # Convert artifacts/runs/20261021_120000/screenshots/TC-001.png
            # → http://server/artifacts/runs/20261021_120000/screenshots/TC-001.png
            relative = screenshot_path
            for prefix in ("artifacts/", "/opt/testing/artifacts/"):
                if screenshot_path.startswith(prefix):
                    relative = screenshot_path[len(prefix):]
                    break
            return f"{self._artifact_server_url}/{relative}"
        # Fall back to file:// for local debugging
        return f"file://{os.path.abspath(screenshot_path)}" if screenshot_path else ""

    def collect_failure_evidence(
        self,
        test_case: TestCase,
        error_message: str,
        classification: FailureClassification,
        duration: float,
        failed_step_index: int = -1,
    ) -> TestResult:
        """Collect all available evidence for a test failure.

        Args:
            test_case: The test case that failed.
            error_message: The error message from the failure.
            classification: The failure classification.
            duration: Test execution duration in seconds.
            failed_step_index: Index of the step that failed (-1 if unknown).

        Returns:
            A TestResult with all available evidence including bug category.
        """
        # Capture screenshot
        screenshot_path = self._executor.capture_screenshot(test_case.test_id)

        # Capture console errors
        console_errors = self._executor.get_console_errors()

        # Capture current URL
        current_url = self._executor.get_current_url()

        # Generate screenshot URL for Grafana
        screenshot_url = self._screenshot_to_url(screenshot_path)

        # Auto-classify bug category
        test_tags = getattr(test_case, "tags", [])
        bug_category = BugCategory.from_failure(
            classification=classification,
            test_tags=test_tags,
            error_message=error_message,
        )

        # Extract failed step description for traceability
        failed_step_description = ""
        if 0 <= failed_step_index < len(test_case.steps):
            step = test_case.steps[failed_step_index]
            failed_step_description = (
                f"Step {failed_step_index + 1}: [{step.action_type.value}] "
                f"{step.description}"
            )

        # Extract source instruction for .testsuite.md traceability
        source_instruction = ""
        source_suite_block = ""
        if isinstance(test_case.test_data, dict):
            source_instruction = test_case.test_data.get("source_instruction", "")
            idx = test_case.test_data.get("source_instruction_index", "")
            if idx:
                source_suite_block = (
                    f"{test_case.page} / {test_case.functionality} — "
                    f"Instruction #{idx}"
                )

        result = TestResult(
            test_id=test_case.test_id,
            test_name=test_case.test_name,
            status=TestStatus.FAIL,
            expected=test_case.expected_result,
            actual=error_message,
            duration_seconds=duration,
            failure_reason=error_message,
            failure_classification=classification,
            bug_category=bug_category,
            screenshot_path=screenshot_path,
            screenshot_url=screenshot_url,
            console_errors=console_errors,
            error_details=error_message,
            page=test_case.page,
            functionality=test_case.functionality,
            url=current_url,
            browser=self._browser,
            project=self._project,
            timestamp=datetime.now(timezone.utc).isoformat(),
            failed_step_description=failed_step_description,
            source_instruction=source_instruction,
            source_suite_block=source_suite_block,
        )

        logger.info(
            "Evidence collected for %s: classification=%s, bug_category=%s, "
            "screenshot=%s, console_errors=%d",
            test_case.test_id,
            classification.value,
            bug_category.value,
            screenshot_path or "none",
            len(console_errors),
        )

        return result

    def create_pass_result(
        self,
        test_case: TestCase,
        duration: float,
    ) -> TestResult:
        """Create a PASS result for a successful test."""
        # Extract source instruction for traceability on passes too
        source_instruction = ""
        source_suite_block = ""
        if isinstance(test_case.test_data, dict):
            source_instruction = test_case.test_data.get("source_instruction", "")
            idx = test_case.test_data.get("source_instruction_index", "")
            if idx:
                source_suite_block = (
                    f"{test_case.page} / {test_case.functionality} — "
                    f"Instruction #{idx}"
                )

        return TestResult(
            test_id=test_case.test_id,
            test_name=test_case.test_name,
            status=TestStatus.PASS,
            expected=test_case.expected_result,
            actual="Test passed",
            duration_seconds=duration,
            page=test_case.page,
            functionality=test_case.functionality,
            url=self._executor.get_current_url(),
            browser=self._browser,
            project=self._project,
            timestamp=datetime.now(timezone.utc).isoformat(),
            source_instruction=source_instruction,
            source_suite_block=source_suite_block,
        )

    def create_skip_result(
        self,
        test_case: TestCase,
        reason: str,
    ) -> TestResult:
        """Create a SKIPPED result."""
        return TestResult(
            test_id=test_case.test_id,
            test_name=test_case.test_name,
            status=TestStatus.SKIPPED,
            expected=test_case.expected_result,
            actual=f"Skipped: {reason}",
            failure_reason=reason,
            page=test_case.page,
            functionality=test_case.functionality,
            browser=self._browser,
            project=self._project,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
