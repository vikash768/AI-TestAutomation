"""
Robot Framework Listener v3 — Prometheus metrics publisher.

This listener intercepts Robot Framework test events and publishes
execution metrics to Prometheus via the MetricsPublisher abstraction.

Usage:
    robot --listener automation/listeners/prometheus_listener.py tests/

The listener is transparent to test code: .robot files have no knowledge
of Prometheus, Pushgateway, or Grafana.

Environment variables consumed:
    PUSHGATEWAY_URL   - Pushgateway endpoint (default: http://pushgateway:9091)
    PROJECT_NAME      - Project identifier for metrics labels (default: poc)
    BROWSER           - Browser name for metrics labels (default: chrome)
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from automation.metrics.publisher import (
    MetricsPublisher,
    NoOpPublisher,
    PrometheusPushgatewayPublisher,
)
from automation.secrets.sanitizer import sanitize_data, sanitize_text
from automation.utilities.test_ids import extract_test_id

# ---------------------------------------------------------------------------
# Configure logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("prometheus_listener")

# ---------------------------------------------------------------------------
# Listener metadata — Robot Framework Listener API v3
# ---------------------------------------------------------------------------


class PrometheusListener:
    """Robot Framework Listener v3 that publishes test metrics."""

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(
        self,
        publisher: MetricsPublisher | None = None,
        run_id: str | None = None,
    ) -> None:
        self._project = sanitize_text(os.environ.get("PROJECT_NAME", "poc"))
        self._browser = sanitize_text(os.environ.get("BROWSER", "chrome"))
        self._test_type = sanitize_text(os.environ.get("TEST_TYPE", "browser"))
        self._run_id = sanitize_text(run_id or f"robot-{uuid.uuid4().hex}")
        self._suite_name = ""
        self._suite_start_time: float = 0.0

        # Per-test tracking
        self._test_results: list[dict] = []
        self._current_test_start: float = 0.0

        # Initialize publisher
        self._publisher = publisher or self._create_publisher()
        log.info(
            "PrometheusListener initialized — project=%s, browser=%s, test_type=%s",
            self._project,
            self._browser,
            self._test_type,
        )

    @property
    def run_id(self) -> str:
        """Stable identifier shared by all metrics from this Robot execution."""
        return self._run_id

    @staticmethod
    def _create_publisher() -> MetricsPublisher:
        """Create the appropriate metrics publisher."""
        pushgateway_url = os.environ.get("PUSHGATEWAY_URL", "")
        if not pushgateway_url:
            log.warning("PUSHGATEWAY_URL not set — using NoOpPublisher")
            return NoOpPublisher()
        try:
            return PrometheusPushgatewayPublisher(pushgateway_url=pushgateway_url)
        except Exception as exc:
            log.error(
                "Failed to create PushgatewayPublisher (%s): %s — falling back to NoOp",
                type(exc).__name__,
                sanitize_text(str(exc)),
            )
            return NoOpPublisher()

    # ------------------------------------------------------------------
    # Listener API v3 hooks
    # ------------------------------------------------------------------

    def start_suite(self, data, result) -> None:
        """Called when a test suite starts."""
        # Use the top-level suite name for metrics
        if not self._suite_name:
            self._suite_name = sanitize_text(data.name or "unknown")
            self._suite_start_time = time.time()
            log.info("Suite started: %s", self._suite_name)

    def start_test(self, data, result) -> None:
        """Called when an individual test starts."""
        self._current_test_start = time.time()
        test_name = sanitize_text(data.name or "unknown")
        test_id = sanitize_text(extract_test_id(test_name))
        log.info("Test started: %s (id=%s)", test_name, test_id)

    def end_test(self, data, result) -> None:
        """Called when an individual test ends."""
        duration = time.time() - self._current_test_start
        test_name = sanitize_text(data.name or "unknown")
        test_id = sanitize_text(extract_test_id(test_name))
        status = sanitize_text(result.status)  # 'PASS', 'FAIL', or 'SKIP'

        self._test_results.append(
            sanitize_data({
                "test_id": test_id,
                "test_name": test_name,
                "status": status,
                "duration": duration,
            })
        )

        self._publisher.publish_test_result(
            project=self._project,
            suite=self._suite_name,
            browser=self._browser,
            test_type=self._test_type,
            test_id=test_id,
            test_name=test_name,
            page="robot",
            functionality="legacy",
            run_id=self._run_id,
            status=status,
            duration_seconds=duration,
        )
        log.info(
            "Test ended: %s — %s (%.2fs)", test_id, status, duration
        )

    def end_suite(self, data, result) -> None:
        """Called when a test suite ends."""
        # Only publish for the top-level suite
        if data.parent is not None:
            return

        suite_duration = time.time() - self._suite_start_time

        total = len(self._test_results)
        passed = sum(1 for r in self._test_results if r["status"] == "PASS")
        failed = sum(1 for r in self._test_results if r["status"] == "FAIL")
        skipped = sum(1 for r in self._test_results if r["status"] == "SKIP")

        self._publisher.publish_suite_result(
            project=self._project,
            suite=self._suite_name,
            browser=self._browser,
            test_type=self._test_type,
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=suite_duration,
        )

        log.info(
            "Suite ended: %s — %d total, %d pass, %d fail, %d skip (%.2fs)",
            self._suite_name,
            total,
            passed,
            failed,
            skipped,
            suite_duration,
        )

    def close(self) -> None:
        """Called when Robot Framework execution finishes. Flush metrics."""
        try:
            self._publisher.flush()
            log.info("Metrics flushed successfully")
        except Exception as exc:
            log.error(
                "Failed to flush metrics (%s): %s",
                type(exc).__name__,
                sanitize_text(str(exc)),
            )
