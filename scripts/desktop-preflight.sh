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
        echo "Set DESKTOP_APP_BUILD_PATH to a .app directory (macOS) or .exe file (Windows)."
        exit 1
    fi
fi

if [ -n "${APP_BUILD_PATH:-}" ] && [ ! -e "$APP_BUILD_PATH" ]; then
    echo "ERROR: Desktop build does not exist: $APP_BUILD_PATH"
    exit 1
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
