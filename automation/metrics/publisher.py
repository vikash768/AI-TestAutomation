"""
MetricsPublisher abstraction and Pushgateway implementation.

The publisher interface is intentionally simple so that future backends
(OpenTelemetry, InfluxDB, Loki, NoOp) can be swapped in without touching
the Robot Listener or test code.
"""

from __future__ import annotations

import abc
import logging
import os
import time
from typing import Any, Optional

from prometheus_client import push_to_gateway

from automation.metrics.metrics import create_metrics, create_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract publisher
# ---------------------------------------------------------------------------


class MetricsPublisher(abc.ABC):
    """Abstract base for publishing QA test execution metrics.

    Implementations must handle:
      - publish_test_result: individual test case result
      - publish_suite_result: aggregated suite-level result
      - flush: ensure all metrics are delivered to the backend
    """

    @abc.abstractmethod
    def publish_test_result(
        self,
        *,
        project: str,
        suite: str,
        browser: str,
        test_type: str,
        test_id: str,
        test_name: str,
        page: str,
        functionality: str,
        run_id: str,
        status: str,
        duration_seconds: float,
    ) -> None:
        """Publish a single test case result."""
        ...

    @abc.abstractmethod
    def publish_suite_result(
        self,
        *,
        project: str,
        suite: str,
        browser: str,
        test_type: str,
        total: int,
        passed: int,
        failed: int,
        skipped: int,
        duration_seconds: float,
    ) -> None:
        """Publish aggregated suite-level metrics."""
        ...

    @abc.abstractmethod
    def flush(self) -> None:
        """Push / finalize all buffered metrics to the backend."""
        ...


# ---------------------------------------------------------------------------
# Pushgateway implementation
# ---------------------------------------------------------------------------

STATUS_MAP = {"PASS": 1, "FAIL": 0, "SKIP": -1}


class PrometheusPushgatewayPublisher(MetricsPublisher):
    """Pushes metrics to Prometheus Pushgateway after test execution.

    Pushgateway grouping key:
        job      = qa_tests
        instance = <project>_<suite>_<browser>

    The runner flushes cumulative metrics after each test for live Grafana
    visibility, then publishes one final aggregate at suite completion.

    Stale metrics: Pushgateway replaces all metrics for a given grouping
    key on each push. Old values are overwritten, not accumulated.
    """

    def __init__(
        self,
        pushgateway_url: Optional[str] = None,
        job_name: str = "qa_tests",
    ):
        self._pushgateway_url = pushgateway_url or os.environ.get(
            "PUSHGATEWAY_URL", "http://pushgateway:9091"
        )
        self._job_name = job_name
        self._registry = create_registry()
        self._metrics = create_metrics(self._registry)
        self._grouping_key: dict[str, str] = {}
        logger.info(
            "PrometheusPushgatewayPublisher initialized — target: %s",
            self._pushgateway_url,
        )

    def publish_test_result(
        self,
        *,
        project: str,
        suite: str,
        browser: str,
        test_type: str,
        test_id: str,
        test_name: str,
        page: str,
        functionality: str,
        run_id: str,
        status: str,
        duration_seconds: float,
    ) -> None:
        labels = {
            "project": project,
            "suite": suite,
            "browser": browser,
            "test_type": test_type,
            "test_id": test_id,
            "test_name": test_name,
            "page": page,
            "functionality": functionality,
            "run_id": run_id,
        }
        numeric_status = STATUS_MAP.get(status.upper(), 0)
        self._metrics["qa_test_case_status"].labels(**labels).set(numeric_status)
        self._metrics["qa_test_case_duration_seconds"].labels(**labels).set(
            duration_seconds
        )
        logger.info(
            "Recorded test result: %s — %s (%.2fs)", test_id, status, duration_seconds
        )

    def publish_suite_result(
        self,
        *,
        project: str,
        suite: str,
        browser: str,
        test_type: str,
        total: int,
        passed: int,
        failed: int,
        skipped: int,
        duration_seconds: float,
    ) -> None:
        labels = {
            "project": project,
            "suite": suite,
            "browser": browser,
            "test_type": test_type,
        }
        self._metrics["qa_test_total"].labels(**labels).set(total)
        self._metrics["qa_test_passed"].labels(**labels).set(passed)
        self._metrics["qa_test_failed"].labels(**labels).set(failed)
        self._metrics["qa_test_skipped"].labels(**labels).set(skipped)

        pass_rate = passed / total if total > 0 else 0.0
        self._metrics["qa_test_pass_rate_ratio"].labels(**labels).set(pass_rate)
        self._metrics["qa_suite_duration_seconds"].labels(**labels).set(
            duration_seconds
        )
        self._metrics["qa_last_run_timestamp_seconds"].labels(**labels).set(
            time.time()
        )

        # Store grouping key for push
        self._grouping_key = {
            "instance": f"{project}_{suite}_{browser}",
        }

        logger.info(
            "Recorded suite result: %d total, %d passed, %d failed, %d skipped, %.2fs",
            total,
            passed,
            failed,
            skipped,
            duration_seconds,
        )

    def flush(self) -> None:
        """Push all collected metrics to Pushgateway."""
        try:
            push_to_gateway(
                gateway=self._pushgateway_url,
                job=self._job_name,
                registry=self._registry,
                grouping_key=self._grouping_key,
            )
            logger.info(
                "Metrics pushed to Pushgateway at %s (job=%s, grouping=%s)",
                self._pushgateway_url,
                self._job_name,
                self._grouping_key,
            )
        except Exception:
            logger.exception("Failed to push metrics to Pushgateway")
            raise


# ---------------------------------------------------------------------------
# NoOp publisher (for debugging without Pushgateway)
# ---------------------------------------------------------------------------


class NoOpPublisher(MetricsPublisher):
    """Logs metrics but does not push anywhere. Useful for local debugging."""

    def publish_test_result(self, **kwargs: Any) -> None:
        logger.info("NoOp — test result: %s", kwargs)

    def publish_suite_result(self, **kwargs: Any) -> None:
        logger.info("NoOp — suite result: %s", kwargs)

    def flush(self) -> None:
        logger.info("NoOp — flush (nothing to send)")
