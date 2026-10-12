"""
Evidence Collector.

Captures diagnostic evidence on test failures:
- Screenshots
- Browser console errors
- Current URL
- Error details
- Failure classification
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from automation.actions.executor import SeleniumExecutor
from automation.actions.schema import (
    FailureClassification,
    TestCase,
    TestResult,
    TestStatus,
)

logger = logging.getLogger(__name__)


class EvidenceCollector:
    """Collects failure evidence from the browser state."""

    def __init__(self, executor: SeleniumExecutor, project: str = "", browser: str = ""):
        self._executor = executor
        self._project = project
        self._browser = browser

    def collect_failure_evidence(
        self,
        test_case: TestCase,
        error_message: str,
        classification: FailureClassification,
        duration: float,
    ) -> TestResult:
        """Collect all available evidence for a test failure.

        Args:
            test_case: The test case that failed.
            error_message: The error message from the failure.
            classification: The failure classification.
            duration: Test execution duration in seconds.

        Returns:
            A TestResult with all available evidence.
        """
        # Capture screenshot
        screenshot_path = self._executor.capture_screenshot(test_case.test_id)

        # Capture console errors
        console_errors = self._executor.get_console_errors()

        # Capture current URL
        current_url = self._executor.get_current_url()

        result = TestResult(
            test_id=test_case.test_id,
            test_name=test_case.test_name,
            status=TestStatus.FAIL,
            expected=test_case.expected_result,
            actual=error_message,
            duration_seconds=duration,
            failure_reason=error_message,
            failure_classification=classification,
            screenshot_path=screenshot_path,
            console_errors=console_errors,
            error_details=error_message,
            page=test_case.page,
            functionality=test_case.functionality,
            url=current_url,
            browser=self._browser,
            project=self._project,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "Evidence collected for %s: classification=%s, screenshot=%s, console_errors=%d",
            test_case.test_id,
            classification.value,
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
