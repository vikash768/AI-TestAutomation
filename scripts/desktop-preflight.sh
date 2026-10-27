#!/usr/bin/env bash
# Native desktop/Appium readiness checks. Sourced by run-suite.sh.

echo "Running desktop/Appium preflight..."

DESKTOP_PLATFORM="${APPIUM_PLATFORM:-}"
case "$DESKTOP_PLATFORM" in
    macos) DRIVER_NAME="mac2" ;;
    windows) DRIVER_NAME="windows" ;;
    *)
        echo "ERROR: Desktop APPIUM_PLATFORM must be macos or windows (got: ${DESKTOP_PLATFORM:-not set})."
        exit 1
        ;;
esac

if [ "${REQUIRE_BUILD_CONTEXT:-false}" = "true" ]; then
    if [ -z "${APP_BUILD_PATH:-}" ] || [ ! -e "$APP_BUILD_PATH" ]; then
        echo "ERROR: Desktop build is required but not found: ${APP_BUILD_PATH:-not set}"
        echo "Set DESKTOP_APP_BUILD_PATH to a .dmg/.app (macOS) or .exe (Windows)."
        exit 1
    fi
fi

if [ -n "${APP_BUILD_PATH:-}" ] && [ ! -e "$APP_BUILD_PATH" ]; then
    echo "ERROR: Desktop build does not exist: $APP_BUILD_PATH"
    exit 1
fi

if [ "$DESKTOP_PLATFORM" = "macos" ]; then
    if [ -n "${APP_BUILD_PATH:-}" ]; then
        APP_LAUNCH_PATH="$($PYTHON_CMD "$PROJECT_DIR/automation/ai/build_inspector.py" --resolve-macos-app "$APP_BUILD_PATH")" || exit 1
        export APP_LAUNCH_PATH
        echo "Launchable app: $APP_LAUNCH_PATH"
    fi
    if ! xcodebuild -version >/dev/null 2>&1; then
        echo "ERROR: Appium Mac2 requires full Xcode. The active developer directory only has Command Line Tools."
        echo "Install Xcode, then run: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
        exit 1
    fi
    # XCTest must enter Automation Mode before it can launch any app. When that
    # needs authentication, macOS shows a password prompt that nothing answers,
    # and Mac2 fails after its 180s launch timeout with "Failed to launch ...".
    if command -v automationmodetool >/dev/null 2>&1 \
        && automationmodetool 2>&1 | grep -q "requires user authentication"; then
        echo "ERROR: macOS Automation Mode requires authentication, so Mac2 cannot launch apps."
        echo "Run once: sudo automationmodetool enable-automationmode-without-authentication"
        exit 1
    fi
fi

if ! command -v appium >/dev/null 2>&1; then
    echo "ERROR: Appium is not installed. Run: npm install -g appium"
    exit 1
fi
if ! appium driver list --installed 2>&1 | grep -q "${DRIVER_NAME}@"; then
    echo "ERROR: Appium ${DRIVER_NAME} driver is missing."
    echo "Run: appium driver install $DRIVER_NAME"
    exit 1
fi

APPIUM_URL="${APPIUM_SERVER_URL:-http://127.0.0.1:4723}"
APPIUM_STATUS_URL="${APPIUM_URL%/}/status"
if ! curl -sf --connect-timeout 2 "$APPIUM_STATUS_URL" >/dev/null 2>&1; then
    case "$APPIUM_URL" in
        http://127.0.0.1:*|http://localhost:*) ;;
        *)
            echo "ERROR: Remote Appium server is not reachable: $APPIUM_URL"
            exit 1
            ;;
    esac
    if [ "${APPIUM_AUTOSTART:-true}" != "true" ]; then
        echo "ERROR: Appium is not running at $APPIUM_URL"
        exit 1
    fi

    APPIUM_PORT="$(printf '%s' "$APPIUM_URL" | sed -E 's#^https?://[^:/]+:([0-9]+).*$#\1#')"
    case "$APPIUM_PORT" in
        ''|*[!0-9]*) APPIUM_PORT=4723 ;;
    esac
    mkdir -p artifacts/logs
    echo "Starting Appium on port $APPIUM_PORT..."
    nohup appium --address 127.0.0.1 --port "$APPIUM_PORT" \
        --log artifacts/logs/appium-desktop.log --log-no-colors --log-timestamp \
        >/dev/null 2>&1 &
    echo $! > artifacts/logs/appium-desktop.pid

    APPIUM_READY=0
    for _attempt in $(seq 1 30); do
        if curl -sf --connect-timeout 2 "$APPIUM_STATUS_URL" >/dev/null 2>&1; then
            APPIUM_READY=1
            break
        fi
        sleep 1
    done
    if [ "$APPIUM_READY" -ne 1 ]; then
        echo "ERROR: Appium did not become ready. See artifacts/logs/appium-desktop.log"
        exit 1
    fi
fi

echo "Appium desktop driver: $DRIVER_NAME"
echo "Appium: ready at $APPIUM_URL"
