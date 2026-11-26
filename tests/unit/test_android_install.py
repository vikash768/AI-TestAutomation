import subprocess
from unittest.mock import Mock

import pytest
from appium import webdriver

from qaplatform.app import device
from qaplatform.app.build import BuildInfo
from qaplatform.config import Settings


@pytest.fixture
def android_startup(monkeypatch):
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device, "ensure_android_device", Mock(return_value="emulator-5554"))
    monkeypatch.setattr(device, "_adb", Mock(return_value="/host/bin/adb"))
    install = Mock(return_value=subprocess.CompletedProcess([], 0, "Performing Streamed Install\nSuccess\n", ""))
    driver = Mock(current_package="com.example.test")
    remote = Mock(return_value=driver)
    monkeypatch.setattr(device.subprocess, "run", install)
    monkeypatch.setattr(webdriver, "Remote", remote)
    return install, remote


@pytest.mark.parametrize("package, activity", [("com.example.test", ".MainActivity"), ("", "")])
def test_android_installs_supplied_apk_before_starting_appium(android_startup, tmp_path, package, activity):
    install, remote = android_startup
    calls = Mock()
    calls.attach_mock(install, "install")
    calls.attach_mock(remote, "remote")
    apk = str(tmp_path / "release build.apk")
    events = Mock()
    build = BuildInfo(platform="android", kind="apk", path=apk, app_id=package, activity=activity)
    app = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, events)

    app.start()

    install.assert_called_once_with(
        ["/host/bin/adb", "-s", "emulator-5554", "install", "-r", apk],
        capture_output=True,
        text=True,
        timeout=device.APK_INSTALL_TIMEOUT_S,
    )
    assert [call[0] for call in calls.mock_calls[:2]] == ["install", "remote"]
    caps = remote.call_args.kwargs["options"].to_capabilities()
    assert caps["appium:udid"] == "emulator-5554"
    assert caps["appium:noReset"] is True and caps["appium:fullReset"] is False
    if package and activity:
        assert caps["appium:appPackage"] == package
        assert caps["appium:appActivity"] == activity
        assert "appium:app" not in caps
    else:
        assert caps["appium:app"] == apk
    assert build.app_id == "com.example.test"
    events.assert_any_call("install", "APK installed on Android device emulator-5554")


def test_android_installed_id_does_not_install_an_apk(android_startup, tmp_path):
    install, remote = android_startup
    build = BuildInfo(platform="android", kind="installed", app_id="com.example.test")
    app = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, Mock())

    app.start()

    install.assert_not_called()
    caps = remote.call_args.kwargs["options"].to_capabilities()
    assert caps["appium:appPackage"] == "com.example.test"
    assert "appium:app" not in caps


@pytest.mark.parametrize("failure", ["INSTALL_FAILED_UPDATE_INCOMPATIBLE", "timeout"])
def test_failed_install_never_starts_the_appium_session(android_startup, tmp_path, failure):
    install, remote = android_startup
    if failure == "timeout":
        install.side_effect = subprocess.TimeoutExpired("adb install", device.APK_INSTALL_TIMEOUT_S)
        message = "timed out"
    else:
        install.return_value = subprocess.CompletedProcess([], 1, "", failure)
        message = failure
    build = BuildInfo(platform="android", kind="apk", path=str(tmp_path / "release.apk"))
    app = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, Mock())

    with pytest.raises(RuntimeError, match=message):
        app.start()

    install.assert_called_once()
    remote.assert_not_called()


def test_android_does_not_fall_back_when_explicit_device_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(device, "_adb", Mock(return_value="/host/bin/adb"))
    monkeypatch.setattr(device.subprocess, "run", Mock(return_value=subprocess.CompletedProcess(
        [], 0, "List of devices attached\nemulator-5554\tdevice\n", "",
    )))

    with pytest.raises(RuntimeError, match="emulator-5556 .*not connected"):
        device.ensure_android_device(Settings(device_name="emulator-5556"), tmp_path, Mock())


def test_android_boots_emulator_from_configured_sdk_when_adb_is_a_wrapper(monkeypatch, tmp_path):
    sdk = tmp_path / "android-sdk"
    emulator = sdk / "emulator" / "emulator"
    emulator.parent.mkdir(parents=True)
    emulator.touch()
    adb = "/homebrew/bin/adb"
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.setattr(device, "_adb", Mock(return_value=adb))
    run = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, "List of devices attached\n\n", ""),
        subprocess.CompletedProcess([], 0, "QA_AVD\n", ""),
        subprocess.CompletedProcess([], 0, "List of devices attached\nphysical\tdevice\nemulator-5554\tdevice\n", ""),
        subprocess.CompletedProcess([], 0, "1\n", ""),
    ])
    boot = Mock()
    monkeypatch.setattr(device.subprocess, "run", run)
    monkeypatch.setattr(device.subprocess, "Popen", boot)

    serial = device.ensure_android_device(Settings(android_avd="QA_AVD"), tmp_path / "logs", Mock())

    assert serial == "emulator-5554"
    assert boot.call_args.args[0] == [str(emulator), "-avd", "QA_AVD", "-no-snapshot-save"]
    assert run.call_args.args[0] == [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"]
