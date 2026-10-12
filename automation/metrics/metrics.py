"""
Prometheus metrics definitions for QA test execution.

All metrics use the 'qa_' namespace and low-cardinality labels only.
Detailed failure data (stack traces, error messages) belongs in Robot artifacts,
NOT in Prometheus labels.
"""

from prometheus_client import CollectorRegistry, Gauge


def create_registry():
    """Create a fresh CollectorRegistry for each test run.

    Using a dedicated registry (not the default) avoids polluting the
    global process metrics and gives us full control over what gets
    pushed to Pushgateway.
    """
    return CollectorRegistry()


def create_metrics(registry: CollectorRegistry) -> dict:
    """Create all QA metrics on the given registry.

    Returns a dict keyed by metric name for easy access.

    Labels used across all suite-level metrics:
        project  - project identifier (e.g. 'poc', 'ijp', 'ecommerce')
        suite    - test suite name (e.g. 'smoke', 'regression')
        browser  - browser name (e.g. 'chrome', 'firefox')
        test_type - type of testing (e.g. 'browser', 'api', 'accessibility')

    Labels used for test-case-level metrics additionally include:
        test_id  - stable test case ID (e.g. 'TC-POC-001')
    """
    suite_labels = ["project", "suite", "browser", "test_type"]
    case_labels = suite_labels + ["test_id", "test_name", "page", "functionality", "run_id"]

    metrics = {
        # --- Suite-level metrics ---
        "qa_test_total": Gauge(
            "qa_test_total",
            "Total number of tests in the suite run",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_test_passed": Gauge(
            "qa_test_passed",
            "Number of passed tests",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_test_failed": Gauge(
            "qa_test_failed",
            "Number of failed tests",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_test_skipped": Gauge(
            "qa_test_skipped",
            "Number of skipped tests",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_test_pass_rate_ratio": Gauge(
            "qa_test_pass_rate_ratio",
            "Pass rate as a ratio between 0.0 and 1.0",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_suite_duration_seconds": Gauge(
            "qa_suite_duration_seconds",
            "Total suite execution duration in seconds",
            labelnames=suite_labels,
            registry=registry,
        ),
        "qa_last_run_timestamp_seconds": Gauge(
            "qa_last_run_timestamp_seconds",
            "Unix epoch timestamp of the last test run",
            labelnames=suite_labels,
            registry=registry,
        ),
        # --- Test-case-level metrics ---
        "qa_test_case_status": Gauge(
            "qa_test_case_status",
            "Test case status: 1=PASS, 0=FAIL, -1=SKIP",
            labelnames=case_labels,
            registry=registry,
        ),
        "qa_test_case_duration_seconds": Gauge(
            "qa_test_case_duration_seconds",
            "Individual test case duration in seconds",
            labelnames=case_labels,
            registry=registry,
        ),
    }

    return metrics
