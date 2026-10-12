"""
Test ID extraction utility.

Extracts stable test IDs (e.g. TC-POC-001) from Robot Framework test names.
Test IDs are used as Prometheus labels and Grafana identifiers, ensuring
traceability from dashboard → test_id → Robot report.

Convention:
    Robot test names start with the test ID:
        "TC-POC-001 Application Opens"
    The extracted ID is:
        "TC-POC-001"
"""

import re

# Pattern: TC-<APP>-<PAGE>-<FUNCTION>-<NUMBER> at the start of the test name
_TEST_ID_PATTERN = re.compile(r"^(TC(?:-[A-Z0-9]+)+(?:-\d{3}))")


def extract_test_id(test_name: str) -> str:
    """Extract the test ID from a Robot Framework test name.

    Args:
        test_name: Full test name, e.g. "TC-POC-001 Application Opens"

    Returns:
        The test ID (e.g. "TC-POC-001"), or the full name sanitized
        as a fallback if no ID pattern is found.

    Examples:
        >>> extract_test_id("TC-POC-001 Application Opens")
        'TC-POC-001'
        >>> extract_test_id("TC-AUTH-002 Invalid Password")
        'TC-AUTH-002'
        >>> extract_test_id("Some Test Without ID")
        'Some_Test_Without_ID'
    """
    match = _TEST_ID_PATTERN.match(test_name.strip())
    if match:
        return match.group(1)
    # Fallback: sanitize the full name for use as a label value
    return re.sub(r"[^a-zA-Z0-9_-]", "_", test_name.strip())
