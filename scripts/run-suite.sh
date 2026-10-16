#!/usr/bin/env bash
# =============================================================================
# Run AI-driven tests for a selected project suite
# =============================================================================
# Usage:
#   ./scripts/run-suite.sh testsuites/myapp.testsuite.md
#   ./scripts/run-suite.sh testsuites/myapp-mobile.testsuite.md
#   ./scripts/run-suite.sh testsuites/myapp-desktop.testsuite.md
#   Or with TEST_SUITE set in .env:
#   ./scripts/run-suite.sh
#
# Environment variables (.env or CLI):
#   TARGET_TYPE         - web (default), mobile, or desktop
#   PROJECT_NAME        - Logical project name
#   APP_REPO_URL        - Optional source repository URL
#   BASE_URL            - Application base URL (web only)
#   BROWSER             - Browser name (web only, default: chrome)
#   APPIUM_SERVER_URL   - Appium server URL (default: http://127.0.0.1:4723)
#   APPIUM_PLATFORM     - android, ios, macos, windows
#   APP_BUILD_PATH      - Path to app build artifact (.apk, .ipa, .app, .exe)
#   DEVICE_NAME         - Device/emulator name
#   APP_PACKAGE         - Android package name
#   APP_ACTIVITY        - Android activity name
#   BUNDLE_ID           - iOS/macOS bundle identifier
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Preserve explicit command-line environment overrides. Sourcing .env directly
# would otherwise replace values selected by Make targets (for example,
# PROJECT_NAME=trackpilot would become PROJECT_NAME=ijustpaid).
CONFIG_VARS=(
    TARGET_TYPE PROJECT_NAME APP_REPO_URL BASE_URL BROWSER SELENIUM_REMOTE_URL
    TEST_SUITE APPIUM_SERVER_URL APPIUM_PLATFORM APP_BUILD_PATH APP_PATH
    DEVICE_NAME APP_PACKAGE APP_ACTIVITY BUNDLE_ID ANDROID_AVD
    VALID_USER_EMAIL VALID_USER_PASSWORD WEB_VALID_USER_EMAIL
    WEB_VALID_USER_PASSWORD MOBILE_VALID_USER_EMAIL MOBILE_VALID_USER_PASSWORD
    AI_MAX_CATEGORIES AI_MAX_TESTS REQUIRE_FULL_MD_COVERAGE MOBILE_RESET_APP
    APP_REPO_REF REQUIRE_REPO_CONTEXT REQUIRE_BUILD_CONTEXT GH_TOKEN GITHUB_TOKEN
    MOBILE_PROJECT_NAME MOBILE_TEST_SUITE MOBILE_APP_REPO_URL MOBILE_APP_REPO_REF
    MOBILE_APP_BUILD_PATH MOBILE_APPIUM_PLATFORM MOBILE_APPIUM_SERVER_URL
    MOBILE_DEVICE_NAME MOBILE_APP_PACKAGE MOBILE_APP_ACTIVITY MOBILE_BUNDLE_ID
    MOBILE_REQUIRE_REPO_CONTEXT MOBILE_REQUIRE_BUILD_CONTEXT
    DESKTOP_PROJECT_NAME DESKTOP_TEST_SUITE DESKTOP_APP_REPO_URL DESKTOP_APP_REPO_REF
    DESKTOP_APP_BUILD_PATH DESKTOP_APPIUM_PLATFORM DESKTOP_APPIUM_SERVER_URL
    DESKTOP_DEVICE_NAME DESKTOP_APP_PACKAGE DESKTOP_APP_ACTIVITY DESKTOP_BUNDLE_ID
    DESKTOP_REQUIRE_REPO_CONTEXT DESKTOP_REQUIRE_BUILD_CONTEXT
)

for _config_var in "${CONFIG_VARS[@]}"; do
    eval "_config_is_set=\${${_config_var}+x}"
    if [ "$_config_is_set" = "x" ]; then
        printf -v "_CLI_${_config_var}" '%s' "${!_config_var}"
        printf -v "_CLI_${_config_var}_SET" '%s' "1"
    fi
done

# Load .env file if present, then restore explicit overrides.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    . "$PROJECT_DIR/.env"
    set +a
fi

for _config_var in "${CONFIG_VARS[@]}"; do
    _set_var="_CLI_${_config_var}_SET"
    if [ "${!_set_var:-}" = "1" ]; then
        _value_var="_CLI_${_config_var}"
        printf -v "$_config_var" '%s' "${!_value_var}"
        export "$_config_var"
    fi
done

# Target type (default: web)
TARGET="${TARGET_TYPE:-web}"

# Resolve target-specific settings. Explicit generic command-line overrides
# always win, preserving backward compatibility with existing CI commands.
apply_target_value() {
    local generic_name="$1"
    local target_name="$2"
    local generic_cli_marker="_CLI_${generic_name}_SET"
    local target_is_set=""
    eval "target_is_set=\${${target_name}+x}"
    if [ "${!generic_cli_marker:-}" != "1" ] && [ "$target_is_set" = "x" ]; then
        printf -v "$generic_name" '%s' "${!target_name}"
        export "$generic_name"
    fi
}

if [ "$TARGET" = "mobile" ]; then
    apply_target_value PROJECT_NAME MOBILE_PROJECT_NAME
    apply_target_value TEST_SUITE MOBILE_TEST_SUITE
    apply_target_value APP_REPO_URL MOBILE_APP_REPO_URL
    apply_target_value APP_REPO_REF MOBILE_APP_REPO_REF
    apply_target_value APP_BUILD_PATH MOBILE_APP_BUILD_PATH
    apply_target_value APPIUM_PLATFORM MOBILE_APPIUM_PLATFORM
    apply_target_value APPIUM_SERVER_URL MOBILE_APPIUM_SERVER_URL
    apply_target_value DEVICE_NAME MOBILE_DEVICE_NAME
    apply_target_value APP_PACKAGE MOBILE_APP_PACKAGE
    apply_target_value APP_ACTIVITY MOBILE_APP_ACTIVITY
    apply_target_value BUNDLE_ID MOBILE_BUNDLE_ID
    apply_target_value REQUIRE_REPO_CONTEXT MOBILE_REQUIRE_REPO_CONTEXT
    apply_target_value REQUIRE_BUILD_CONTEXT MOBILE_REQUIRE_BUILD_CONTEXT
elif [ "$TARGET" = "desktop" ]; then
    apply_target_value PROJECT_NAME DESKTOP_PROJECT_NAME
    apply_target_value TEST_SUITE DESKTOP_TEST_SUITE
    apply_target_value APP_REPO_URL DESKTOP_APP_REPO_URL
    apply_target_value APP_REPO_REF DESKTOP_APP_REPO_REF
    apply_target_value APP_BUILD_PATH DESKTOP_APP_BUILD_PATH
    apply_target_value APPIUM_PLATFORM DESKTOP_APPIUM_PLATFORM
    apply_target_value APPIUM_SERVER_URL DESKTOP_APPIUM_SERVER_URL
    apply_target_value DEVICE_NAME DESKTOP_DEVICE_NAME
    apply_target_value APP_PACKAGE DESKTOP_APP_PACKAGE
    apply_target_value APP_ACTIVITY DESKTOP_APP_ACTIVITY
    apply_target_value BUNDLE_ID DESKTOP_BUNDLE_ID
    apply_target_value REQUIRE_REPO_CONTEXT DESKTOP_REQUIRE_REPO_CONTEXT
    apply_target_value REQUIRE_BUILD_CONTEXT DESKTOP_REQUIRE_BUILD_CONTEXT
fi

# Keep web and mobile credentials independent while retaining compatibility
# with the original VALID_USER_* variables.
if [ "$TARGET" = "web" ]; then
    [ -n "${TRACKPILOT_BASE_URL:-}" ] && export BASE_URL="$TRACKPILOT_BASE_URL"
    [ -n "${TRACKPILOT_ADMIN_EMAIL:-}" ] && export WEB_VALID_USER_EMAIL="$TRACKPILOT_ADMIN_EMAIL"
    [ -n "${TRACKPILOT_ADMIN_PASSWORD:-}" ] && export WEB_VALID_USER_PASSWORD="$TRACKPILOT_ADMIN_PASSWORD"
    [ -n "${WEB_VALID_USER_EMAIL:-}" ] && export VALID_USER_EMAIL="$WEB_VALID_USER_EMAIL"
    [ -n "${WEB_VALID_USER_PASSWORD:-}" ] && export VALID_USER_PASSWORD="$WEB_VALID_USER_PASSWORD"
elif [ "$TARGET" = "mobile" ]; then
    [ -n "${MOBILE_VALID_USER_EMAIL:-}" ] && export VALID_USER_EMAIL="$MOBILE_VALID_USER_EMAIL"
    [ -n "${MOBILE_VALID_USER_PASSWORD:-}" ] && export VALID_USER_PASSWORD="$MOBILE_VALID_USER_PASSWORD"
fi

# Detect Python executable on host
PYTHON_CMD="python3"
command -v python3 >/dev/null 2>&1 || PYTHON_CMD="python"

# Auto-detect ANDROID_HOME if not set
if [ -z "${ANDROID_HOME:-}" ]; then
    if [ -d "$HOME/Library/Android/sdk" ]; then
        export ANDROID_HOME="$HOME/Library/Android/sdk"
        export ANDROID_SDK_ROOT="$HOME/Library/Android/sdk"
        export PATH="$PATH:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"
    fi
fi

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    echo "AI Testing Automation — Suite Runner"
    echo ""
    echo "Usage: $0 [path/to/suite.testsuite.md]"
    echo ""
    echo "Runs AI-driven tests for web, mobile, or desktop applications."
    echo "All configuration is loaded from .env or environment variables."
    echo ""
    echo "Key Environment Variables (.env):"
    echo "  TARGET_TYPE         web (default), mobile, or desktop"
    echo "  PROJECT_NAME        Logical project name"
    echo "  APP_BUILD_PATH      Path to .apk, .ipa, .app, or .exe"
    echo "  APPIUM_PLATFORM     android, ios, macos, windows"
    echo "  APPIUM_SERVER_URL   Appium endpoint (default: http://127.0.0.1:4723)"
    echo "  BASE_URL            Web application URL (web only)"
    echo "  TEST_SUITE          Default suite path"
    echo ""
    echo "Available test suites in testsuites/:"
    ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
    exit 0
fi

# Suite file (from argument or TEST_SUITE environment variable)
SUITE_FILE="${1:-${TEST_SUITE:-}}"

if [ -z "$SUITE_FILE" ]; then
    echo "ERROR: No test suite specified. Provide suite file or set TEST_SUITE in .env."
    echo ""
    echo "Usage: $0 <path-to-suite.testsuite.md>"
    echo ""
    echo "Available suites:"
    ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
    exit 1
fi

if [ ! -f "$SUITE_FILE" ]; then
    echo "ERROR: Suite file not found: $SUITE_FILE"
    echo ""
    echo "Available suites:"
    ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
    exit 1
fi

APP_BUILD="${APP_BUILD_PATH:-${APP_PATH:-}}"

echo "=== AI Testing Automation — Running Suite ==="
echo ""
echo "  Suite:    $SUITE_FILE"
echo "  Project:  ${PROJECT_NAME:-not set}"
echo "  Target:   $TARGET"
[ -n "${AI_MAX_CATEGORIES:-}" ] && echo "  Demo max: ${AI_MAX_CATEGORIES} test categor$( [ "$AI_MAX_CATEGORIES" = "1" ] && printf 'y' || printf 'ies' )"
[ -n "${AI_MAX_TESTS:-}" ] && echo "  Test max: ${AI_MAX_TESTS} generated test(s)"
if [ "$TARGET" = "web" ]; then
    echo "  Browser:  ${BROWSER:-chrome}"
    echo "  URL:      ${BASE_URL:-not set}"
else
    echo "  Platform: ${APPIUM_PLATFORM:-not set}"
    echo "  Appium:   ${APPIUM_SERVER_URL:-http://127.0.0.1:4723}"
    echo "  App:      ${APP_BUILD:-not set}"
    [ -n "${APP_PACKAGE:-}" ]  && echo "  Package:  $APP_PACKAGE"
    [ -n "${APP_ACTIVITY:-}" ] && echo "  Activity: $APP_ACTIVITY"
    [ -n "${BUNDLE_ID:-}" ]    && echo "  Bundle:   $BUNDLE_ID"
fi
[ -n "${APP_REPO_URL:-}" ]     && echo "  Repo:     $APP_REPO_URL"
[ "${REQUIRE_REPO_CONTEXT:-false}" = "true" ] && echo "  Repo mode: required"
[ "${REQUIRE_BUILD_CONTEXT:-false}" = "true" ] && echo "  Build mode: required"
echo ""

# Create artifact directories
mkdir -p artifacts/runs artifacts/screenshots artifacts/logs

# Resolve the app artifact after target-specific preflight may update variables.
APP_BUILD="${APP_BUILD_PATH:-${APP_PATH:-}}"

if [ "$TARGET" = "web" ]; then
    # --- Web execution: run inside Docker with Selenium ---
    if [ -z "${BASE_URL:-}" ]; then
        echo "ERROR: BASE_URL is required for Selenium web testing."
        exit 1
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: Docker is required for Selenium web testing."
        exit 1
    fi

    echo "Building test runner..."
    docker compose build test-runner 2>&1 | tail -3

    echo "Starting/checking Selenium and Grafana metrics stack..."
    docker compose up -d selenium pushgateway prometheus grafana
    SELENIUM_READY=0
    for _attempt in $(seq 1 30); do
        if curl -sf --connect-timeout 2 \
            "http://localhost:${SELENIUM_PORT:-4444}/wd/hub/status" >/dev/null 2>&1; then
            SELENIUM_READY=1
            break
        fi
        sleep 1
    done
    if [ "$SELENIUM_READY" -ne 1 ]; then
        echo "ERROR: Selenium did not become ready on port ${SELENIUM_PORT:-4444}."
        echo "Run: docker compose logs selenium"
        exit 1
    fi

    echo ""
    echo "Starting test execution (web)..."
    echo "Watch live: http://localhost:${SELENIUM_VNC_PORT:-7900} (password: secret)"
    echo ""

    RUNNER_ARGS=(--suite "$SUITE_FILE" --target-type "$TARGET" --artifacts /opt/testing/artifacts)
    [ -n "${PROJECT_NAME:-}" ] && RUNNER_ARGS+=(--project "$PROJECT_NAME")
    [ -n "${BROWSER:-}" ]      && RUNNER_ARGS+=(--browser "$BROWSER")
    [ -n "${BASE_URL:-}" ]     && RUNNER_ARGS+=(--base-url "$BASE_URL")
    [ -n "${APP_REPO_URL:-}" ] && RUNNER_ARGS+=(--app-repo-url "$APP_REPO_URL")

    # Run the AI test runner inside the Docker container
    if docker compose run --rm \
        -e TEST_SUITE="$SUITE_FILE" \
        -e TARGET_TYPE="$TARGET" \
        -e BASE_URL="${BASE_URL:-}" \
        -e VALID_USER_EMAIL="${VALID_USER_EMAIL:-}" \
        -e VALID_USER_PASSWORD="${VALID_USER_PASSWORD:-}" \
        -e AI_MAX_CATEGORIES="${AI_MAX_CATEGORIES:-}" \
        -e AI_MAX_TESTS="${AI_MAX_TESTS:-}" \
        -e REQUIRE_FULL_MD_COVERAGE="${REQUIRE_FULL_MD_COVERAGE:-false}" \
        test-runner \
        python -m automation.runner "${RUNNER_ARGS[@]}"; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi
else
    # --- Desktop/Mobile execution: run on host against Appium ---
    if [ "$TARGET" = "mobile" ] && [ "${APPIUM_PLATFORM:-}" = "android" ]; then
        # Source so an auto-detected emulator serial is available below.
        . "$PROJECT_DIR/scripts/mobile-preflight.sh"
    elif [ "$TARGET" = "desktop" ]; then
        . "$PROJECT_DIR/scripts/desktop-preflight.sh"
    fi

    echo "Starting test execution ($TARGET)..."
    echo ""

    # Build CLI arguments for Appium options
    APPIUM_ARGS=()
    [ -n "${APPIUM_SERVER_URL:-}" ] && APPIUM_ARGS+=(--appium-server-url "$APPIUM_SERVER_URL")
    [ -n "${APPIUM_PLATFORM:-}" ]   && APPIUM_ARGS+=(--appium-platform "$APPIUM_PLATFORM")
    [ -n "$APP_BUILD" ]             && APPIUM_ARGS+=(--app-build-path "$APP_BUILD")
    [ -n "${DEVICE_NAME:-}" ]       && APPIUM_ARGS+=(--device-name "$DEVICE_NAME")
    [ -n "${APP_PACKAGE:-}" ]       && APPIUM_ARGS+=(--app-package "$APP_PACKAGE")
    [ -n "${APP_ACTIVITY:-}" ]      && APPIUM_ARGS+=(--app-activity "$APP_ACTIVITY")
    [ -n "${BUNDLE_ID:-}" ]         && APPIUM_ARGS+=(--bundle-id "$BUNDLE_ID")
    [ -n "${PROJECT_NAME:-}" ]      && APPIUM_ARGS+=(--project "$PROJECT_NAME")
    [ -n "${APP_REPO_URL:-}" ]      && APPIUM_ARGS+=(--app-repo-url "$APP_REPO_URL")

    if $PYTHON_CMD -m automation.runner \
        --suite "$SUITE_FILE" \
        --target-type "$TARGET" \
        --artifacts "artifacts" \
        "${APPIUM_ARGS[@]}"; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi
fi

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "=== All tests PASSED ✅ ==="
else
    echo "=== Some tests FAILED ❌ (exit code: $EXIT_CODE) ==="
fi

echo "Results: artifacts/runs/"
[ "$TARGET" = "web" ] && echo "Live browser: http://localhost:${SELENIUM_VNC_PORT:-7900}"
echo "Grafana: http://localhost:${GRAFANA_PORT:-3000}"

exit $EXIT_CODE
