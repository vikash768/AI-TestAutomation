#!/usr/bin/env bash
# =============================================================================
# Run AI-driven tests for a selected project suite
# =============================================================================
# Usage:
#   ./scripts/run-suite.sh                                    # default: trackpilot
#   ./scripts/run-suite.sh testsuites/trackpilot.testsuite.md
#   ./scripts/run-suite.sh testsuites/ijp.testsuite.md
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Suite file (default: trackpilot)
SUITE_FILE="${1:-testsuites/trackpilot.testsuite.md}"

if [ ! -f "$SUITE_FILE" ]; then
    echo "ERROR: Suite file not found: $SUITE_FILE"
    echo ""
    echo "Available suites:"
    ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
    exit 1
fi

echo "=== AI Testing Automation — Running Suite ==="
echo ""
echo "  Suite:   $SUITE_FILE"
echo "  Project: ${PROJECT_NAME:-trackpilot}"
echo "  Browser: ${BROWSER:-chrome}"
echo "  Target:  ${BASE_URL:-https://trackpilot.pods365.com}"
echo ""

# Create artifact directories
mkdir -p artifacts/runs artifacts/screenshots artifacts/logs

# Build test runner if needed
echo "Building test runner..."
docker compose build test-runner 2>&1 | tail -3

echo ""
echo "Starting test execution..."
echo ""

# Run the AI test runner inside the Docker container
docker compose run --rm \
    -e TEST_SUITE="$SUITE_FILE" \
    test-runner \
    python -m automation.runner \
        --suite "$SUITE_FILE" \
        --project "${PROJECT_NAME:-trackpilot}" \
        --browser "${BROWSER:-chrome}" \
        --base-url "${BASE_URL:-https://trackpilot.pods365.com}" \
        --artifacts "/opt/testing/artifacts"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "=== All tests PASSED ✅ ==="
else
    echo "=== Some tests FAILED ❌ (exit code: $EXIT_CODE) ==="
fi

echo ""
echo "Results: artifacts/runs/"
echo "Grafana:  http://localhost:${GRAFANA_PORT:-3000}"
echo ""

exit $EXIT_CODE
