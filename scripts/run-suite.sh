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
#   APP_BUILD_PATH      - Path to app build artifact (.apk, .ipa, .dmg, .app, .exe)
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
# PROJECT_NAME=trackpilot would be replaced by the .env PROJECT_NAME).
CONFIG_VARS=(
    TARGET_TYPE PROJECT_NAME APP_REPO_URL REPO_URL PLATFORM BASE_URL WEB_BASE_URL BROWSER SELENIUM_REMOTE_URL
    TEST_SUITE APPIUM_SERVER_URL APPIUM_PLATFORM APP_BUILD_PATH APP_PATH
    DEVICE_NAME APP_PACKAGE APP_ACTIVITY BUNDLE_ID ANDROID_AVD
    VALID_USER_EMAIL VALID_USER_PASSWORD WEB_PROJECT_NAME WEB_TEST_SUITE WEB_APP_REPO_URL
    WEB_APP_REPO_LOCAL_PATH
    WEB_VALID_USER_EMAIL
    WEB_VALID_USER_PASSWORD MOBILE_VALID_USER_EMAIL MOBILE_VALID_USER_PASSWORD
    AI_MAX_CATEGORIES AI_MAX_TESTS REQUIRE_FULL_MD_COVERAGE MOBILE_RESET_APP
    APP_REPO_REF APP_REPO_LOCAL_PATH REQUIRE_REPO_CONTEXT REQUIRE_BUILD_CONTEXT GH_TOKEN GITHUB_TOKEN
    MOBILE_PROJECT_NAME MOBILE_TEST_SUITE MOBILE_APP_REPO_URL MOBILE_APP_REPO_REF MOBILE_APP_REPO_LOCAL_PATH
    MOBILE_APP_BUILD_PATH MOBILE_APPIUM_PLATFORM MOBILE_APPIUM_SERVER_URL
    MOBILE_DEVICE_NAME MOBILE_APP_PACKAGE MOBILE_APP_ACTIVITY MOBILE_BUNDLE_ID
    MOBILE_REQUIRE_REPO_CONTEXT MOBILE_REQUIRE_BUILD_CONTEXT
    DESKTOP_PROJECT_NAME DESKTOP_TEST_SUITE DESKTOP_APP_REPO_URL DESKTOP_APP_REPO_REF DESKTOP_APP_REPO_LOCAL_PATH
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

apply_target_config() {
    if [ "$TARGET" = "web" ]; then
        apply_target_value PROJECT_NAME WEB_PROJECT_NAME
        apply_target_value TEST_SUITE WEB_TEST_SUITE
        apply_target_value APP_REPO_URL WEB_APP_REPO_URL
        apply_target_value APP_REPO_LOCAL_PATH WEB_APP_REPO_LOCAL_PATH
        if [ -n "${WEB_BASE_URL:-}" ]; then
            apply_target_value BASE_URL WEB_BASE_URL
        fi
    elif [ "$TARGET" = "mobile" ]; then
        apply_target_value PROJECT_NAME MOBILE_PROJECT_NAME
        apply_target_value TEST_SUITE MOBILE_TEST_SUITE
        apply_target_value APP_REPO_URL MOBILE_APP_REPO_URL
        apply_target_value APP_REPO_REF MOBILE_APP_REPO_REF
        apply_target_value APP_REPO_LOCAL_PATH MOBILE_APP_REPO_LOCAL_PATH
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
        apply_target_value APP_REPO_LOCAL_PATH DESKTOP_APP_REPO_LOCAL_PATH
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
}

apply_target_config

# A new project URL/build supplied for this run must not inherit a local
# checkout, bundle ID, or metrics name from the previous .env project.
if [ "${_CLI_APP_REPO_URL_SET:-}" = "1" ]; then
    if [ "${_CLI_APP_REPO_LOCAL_PATH_SET:-}" != "1" ]; then
        APP_REPO_LOCAL_PATH=""
        export APP_REPO_LOCAL_PATH
    fi
    if [ "${_CLI_PROJECT_NAME_SET:-}" != "1" ]; then
        _project_slug="${APP_REPO_URL##*/}"
        PROJECT_NAME="${_project_slug%.git}"
        export PROJECT_NAME
    fi
fi
if [ "${_CLI_APP_BUILD_PATH_SET:-}" = "1" ] && [ "${_CLI_BUNDLE_ID_SET:-}" != "1" ]; then
    BUNDLE_ID=""
    export BUNDLE_ID
fi

# Keep web and mobile credentials independent while retaining compatibility
# with the original VALID_USER_* variables.
if [ "$TARGET" = "web" ]; then
    if [ "${_CLI_BASE_URL_SET:-}" != "1" ] && [ -z "${WEB_BASE_URL:-}" ] && [ "${PROJECT_NAME:-}" = "trackpilot" ] && [ -n "${TRACKPILOT_BASE_URL:-}" ]; then
        export BASE_URL="$TRACKPILOT_BASE_URL"
    fi
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

# Parse command line options
SUITE_FILE=""
ARG_TARGET_TYPE_SET=0
ARG_PLATFORM_SET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            echo "AI Testing Automation — Suite Runner"
            echo ""
            echo "Usage: $0 [path/to/suite.testsuite.md]"
            echo "       $0 --repo-url <url> --platform <platform>"
            echo ""
            echo "Runs AI-driven tests for web, mobile, or desktop applications."
            echo "All configuration is loaded from .env or environment variables."
            echo ""
            echo "Key Environment Variables (.env):"
            echo "  TARGET_TYPE         web (default), mobile, or desktop"
            echo "  PROJECT_NAME        Logical project name"
            echo "  APP_REPO_URL        Source repository URL"
            echo "  PLATFORM            web, android, ios, macos, windows"
            echo "  APP_BUILD_PATH      Path to .apk, .ipa, .app, or .exe"
            echo "  APPIUM_PLATFORM     android, ios, macos, windows"
            echo "  APPIUM_SERVER_URL   Appium endpoint (default: http://127.0.0.1:4723)"
            echo "  BASE_URL            Web application URL (web only)"
            echo "  TEST_SUITE          Default suite path"
            echo ""
            echo "Available test suites in testsuites/:"
            ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
            exit 0
            ;;
        --repo-url|--repo)
            APP_REPO_URL="$2"
            export APP_REPO_URL
            shift 2
            ;;
        --platform)
            PLATFORM="$2"
            export PLATFORM
            ARG_PLATFORM_SET=1
            shift 2
            ;;
        --suite)
            SUITE_FILE="$2"
            shift 2
            ;;
        --target-type)
            TARGET="$2"
            export TARGET_TYPE="$2"
            ARG_TARGET_TYPE_SET=1
            shift 2
            ;;
        *)
            if [ -z "$SUITE_FILE" ]; then
                SUITE_FILE="$1"
            fi
            shift
            ;;
    esac
done

# Fallback to TEST_SUITE env var if suite wasn't given on CLI
if [ -z "$SUITE_FILE" ]; then
    SUITE_FILE="${TEST_SUITE:-}"
fi

# Apply PLATFORM mapping if it was selected explicitly, or when no explicit
# TARGET_TYPE was supplied. A generic PLATFORM value loaded from .env must not
# turn `make test-desktop` into an Android/mobile run.
TARGET_TYPE_EXPLICIT="${_CLI_TARGET_TYPE_SET:-0}"
PLATFORM_EXPLICIT="${_CLI_PLATFORM_SET:-0}"
[ "$ARG_TARGET_TYPE_SET" = "1" ] && TARGET_TYPE_EXPLICIT=1
[ "$ARG_PLATFORM_SET" = "1" ] && PLATFORM_EXPLICIT=1

if [ -n "${PLATFORM:-}" ] && [ "$TARGET_TYPE_EXPLICIT" = "1" ] && [ "$PLATFORM_EXPLICIT" != "1" ]; then
    # Ignore a stale generic platform from .env; target-specific Appium
    # settings below remain authoritative.
    PLATFORM=""
    export PLATFORM
fi

if [ -n "${PLATFORM:-}" ]; then
    PLATFORM_TARGET=""
    case "$PLATFORM" in
        web)
            PLATFORM_TARGET="web"
            ;;
        android)
            PLATFORM_TARGET="mobile"
            export APPIUM_PLATFORM="android"
            ;;
        ios)
            PLATFORM_TARGET="mobile"
            export APPIUM_PLATFORM="ios"
            ;;
        macos)
            PLATFORM_TARGET="desktop"
            export APPIUM_PLATFORM="macos"
            ;;
        windows)
            PLATFORM_TARGET="desktop"
            export APPIUM_PLATFORM="windows"
            ;;
        *)
            echo "ERROR: Unsupported PLATFORM '$PLATFORM'. Use web, android, ios, macos, or windows."
            exit 1
            ;;
    esac

    if [ "$TARGET_TYPE_EXPLICIT" = "1" ] && [ "$TARGET" != "$PLATFORM_TARGET" ]; then
        echo "ERROR: TARGET_TYPE=$TARGET conflicts with PLATFORM=$PLATFORM."
        echo "Use a desktop platform (macos/windows) for desktop tests or clear the generic PLATFORM value."
        exit 1
    fi
    TARGET="$PLATFORM_TARGET"
    export TARGET_TYPE="$TARGET"
fi

# Command-line target/platform options are parsed after .env loading, so apply
# the matching target namespace once more using the final resolved target.
apply_target_config

# Ensure either a suite or a repo is provided
if [ -z "$SUITE_FILE" ] && [ -z "${APP_REPO_URL:-}" ]; then
    echo "ERROR: No test suite or repository URL specified."
    echo ""
    echo "Usage: $0 <path-to-suite.testsuite.md>"
    echo "   or: $0 --repo-url <url> --platform <platform>"
    echo ""
    echo "Available suites in testsuites/:"
    ls -1 testsuites/*.testsuite.md 2>/dev/null || echo "  (none found)"
    exit 1
fi

if [ -n "$SUITE_FILE" ] && [ ! -f "$SUITE_FILE" ] && [ -z "${APP_REPO_URL:-}" ]; then
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

    if command -v docker >/dev/null 2>&1; then
        echo "Starting/checking Grafana metrics stack..."
        if ! docker compose up -d pushgateway prometheus grafana; then
            echo "WARNING: Metrics stack could not start; test artifacts will still be saved locally."
        fi
    fi

    echo "Starting test execution ($TARGET)..."
    echo ""

    # Build CLI arguments
    RUNNER_ARGS=()
    [ -n "$SUITE_FILE" ] && RUNNER_ARGS+=(--suite "$SUITE_FILE")
    [ -n "${TARGET:-}" ] && RUNNER_ARGS+=(--target-type "$TARGET")
    [ -n "${PLATFORM:-}" ] && RUNNER_ARGS+=(--platform "$PLATFORM")
    RUNNER_ARGS+=(--artifacts "artifacts")
    [ -n "${APPIUM_SERVER_URL:-}" ] && RUNNER_ARGS+=(--appium-server-url "$APPIUM_SERVER_URL")
    [ -n "${APPIUM_PLATFORM:-}" ]   && RUNNER_ARGS+=(--appium-platform "$APPIUM_PLATFORM")
    [ -n "$APP_BUILD" ]             && RUNNER_ARGS+=(--app-build-path "$APP_BUILD")
    [ -n "${DEVICE_NAME:-}" ]       && RUNNER_ARGS+=(--device-name "$DEVICE_NAME")
    [ -n "${APP_PACKAGE:-}" ]       && RUNNER_ARGS+=(--app-package "$APP_PACKAGE")
    [ -n "${APP_ACTIVITY:-}" ]      && RUNNER_ARGS+=(--app-activity "$APP_ACTIVITY")
    [ -n "${BUNDLE_ID:-}" ]         && RUNNER_ARGS+=(--bundle-id "$BUNDLE_ID")
    [ -n "${PROJECT_NAME:-}" ]      && RUNNER_ARGS+=(--project "$PROJECT_NAME")
    [ -n "${APP_REPO_URL:-}" ]      && RUNNER_ARGS+=(--repo-url "$APP_REPO_URL")
    [ -n "${APP_REPO_LOCAL_PATH:-}" ] && RUNNER_ARGS+=(--app-repo-local-path "$APP_REPO_LOCAL_PATH")

    if $PYTHON_CMD -m automation.runner "${RUNNER_ARGS[@]}"; then
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
