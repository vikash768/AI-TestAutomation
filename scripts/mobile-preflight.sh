#!/usr/bin/env bash
# Android/Appium readiness checks used by run-suite.sh.
# This file is sourced so DEVICE_NAME can be updated after emulator discovery.

echo "Running Android/Appium preflight..."

if [ "${REQUIRE_BUILD_CONTEXT:-false}" = "true" ]; then
    if [ -z "${APP_BUILD_PATH:-}" ] || [ ! -f "$APP_BUILD_PATH" ]; then
        echo "ERROR: Mobile build is required but not found: ${APP_BUILD_PATH:-not set}"
        echo "Set MOBILE_APP_BUILD_PATH to the application APK path."
        exit 1
    fi
fi

if ! command -v appium >/dev/null 2>&1; then
    echo "ERROR: Appium is not installed. Run: npm install -g appium"
    exit 1
fi
if ! command -v adb >/dev/null 2>&1; then
    echo "ERROR: adb is not available. Install Android SDK platform-tools."
    exit 1
fi
if ! appium driver list --installed 2>&1 | grep -q 'uiautomator2@'; then
    echo "ERROR: Appium UiAutomator2 driver is missing."
    echo "Run: appium driver install uiautomator2"
    exit 1
fi

adb start-server >/dev/null

device_is_ready() {
    [ -n "${1:-}" ] && adb devices | awk -v serial="$1" '$1 == serial && $2 == "device" { found=1 } END { exit !found }'
}

if ! device_is_ready "${DEVICE_NAME:-}"; then
    DETECTED_DEVICE="$(adb devices | awk 'NR > 1 && $2 == "device" { print $1; exit }')"
    if [ -n "$DETECTED_DEVICE" ]; then
        DEVICE_NAME="$DETECTED_DEVICE"
    else
        if [ "${ANDROID_EMULATOR_AUTOSTART:-true}" != "true" ]; then
            echo "ERROR: No Android device is connected."
            exit 1
        fi

        EMULATOR_BIN="${ANDROID_HOME:-$HOME/Library/Android/sdk}/emulator/emulator"
        if [ ! -x "$EMULATOR_BIN" ]; then
            echo "ERROR: Android emulator not found at $EMULATOR_BIN"
            exit 1
        fi

        AVAILABLE_AVDS="$($EMULATOR_BIN -list-avds)"
        AVD_NAME="${ANDROID_AVD:-$(printf '%s\n' "$AVAILABLE_AVDS" | sed '/^[[:space:]]*$/d' | head -1)}"
        if [ -z "$AVD_NAME" ]; then
            echo "ERROR: No Android Virtual Device is configured."
            exit 1
        fi

        mkdir -p artifacts/logs
        echo "Starting Android emulator: $AVD_NAME"
        nohup "$EMULATOR_BIN" -avd "$AVD_NAME" -no-snapshot-save \
            > artifacts/logs/emulator.log 2>&1 &
        echo $! > artifacts/logs/emulator.pid

        DEVICE_NAME=""
        for _attempt in $(seq 1 90); do
            DEVICE_NAME="$(adb devices | awk 'NR > 1 && $2 == "device" { print $1; exit }')"
            [ -n "$DEVICE_NAME" ] && break
            sleep 2
        done
        if [ -z "$DEVICE_NAME" ]; then
            echo "ERROR: Android emulator did not connect. See artifacts/logs/emulator.log"
            exit 1
        fi
    fi
fi

export DEVICE_NAME
echo "Android device: $DEVICE_NAME"

BOOT_COMPLETE=""
for _attempt in $(seq 1 60); do
    BOOT_COMPLETE="$(adb -s "$DEVICE_NAME" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
    [ "$BOOT_COMPLETE" = "1" ] && break
    sleep 2
done
if [ "$BOOT_COMPLETE" != "1" ]; then
    echo "ERROR: Android device did not finish booting."
    exit 1
fi

if [ -n "${APP_PACKAGE:-}" ] && ! adb -s "$DEVICE_NAME" shell pm path "$APP_PACKAGE" >/dev/null 2>&1; then
    if [ -n "${APP_BUILD_PATH:-}" ] && [ -f "$APP_BUILD_PATH" ]; then
        echo "Installing application APK..."
        adb -s "$DEVICE_NAME" install -r "$APP_BUILD_PATH" >/dev/null
    else
        echo "ERROR: $APP_PACKAGE is not installed and APP_BUILD_PATH is unavailable."
        exit 1
    fi
fi

if [ "${MOBILE_RESET_APP:-false}" = "true" ] && [ -n "${APP_PACKAGE:-}" ]; then
    echo "Resetting $APP_PACKAGE to a clean demo state..."
    adb -s "$DEVICE_NAME" shell pm clear "$APP_PACKAGE" >/dev/null
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
        --log artifacts/logs/appium.log --log-no-colors --log-timestamp \
        >/dev/null 2>&1 &
    echo $! > artifacts/logs/appium.pid

    APPIUM_READY=0
    for _attempt in $(seq 1 30); do
        if curl -sf --connect-timeout 2 "$APPIUM_STATUS_URL" >/dev/null 2>&1; then
            APPIUM_READY=1
            break
        fi
        sleep 1
    done
    if [ "$APPIUM_READY" -ne 1 ]; then
        echo "ERROR: Appium did not become ready. See artifacts/logs/appium.log"
        tail -30 artifacts/logs/appium.log 2>/dev/null || true
        exit 1
    fi
fi

echo "Appium: ready at $APPIUM_URL"
