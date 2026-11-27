import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from appium import webdriver

from qaplatform.app import build as builds
from qaplatform.app import device
from qaplatform.app.build import BuildInfo, resolve_build
from qaplatform.app.checks import ActiveChecks
from qaplatform.app.explore import AppExplorer, Screen, Tap
from qaplatform.config import Settings
from qaplatform.explore.crawler import ObservedElement


def write_bundle(path: Path) -> Path:
    contents = path / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.example.desktop",
        "CFBundleDisplayName": "Test Desktop",
        "CFBundleShortVersionString": "1.2",
    }))
    return path


@pytest.fixture(autouse=True)
def mac_runner_port(monkeypatch):
    allocate = Mock(side_effect=[50120, 50121])
    monkeypatch.setattr(device, "_macos_runner_port", allocate)
    monkeypatch.setattr(device, "_stop_macos_runner", Mock())
    return allocate


@pytest.mark.parametrize("kind, expected_skip_kill", [("app", True), ("electron", True), ("installed", True), ("dmg", False)])
def test_mac_session_selects_requested_build_and_preserves_installed_apps(tmp_path, monkeypatch, kind, expected_skip_kill):
    app = write_bundle(tmp_path / "Test Desktop.app")
    if kind == "installed":
        build = resolve_build("com.example.desktop", "macos")
    else:
        build = resolve_build(str(app), "macos")
    if kind == "dmg":
        build.kind = kind
        build._dmg_device = "/dev/disk99"
    if kind == "electron":
        build.framework = "electron"
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    signature = Mock(return_value=True)
    monkeypatch.setattr(device, "_has_invalid_macos_signature", signature)
    checked = Mock(return_value="foreground")
    monkeypatch.setattr(device, "_macos_launch_status", checked)
    launch = Mock()
    monkeypatch.setattr(device.subprocess, "run", launch)
    driver = Mock()
    driver.execute_script.return_value = 4

    def connect(*args, **kwargs):
        if kind != "dmg":
            checked.assert_called_once_with(build, 60)
        return driver

    remote = Mock(side_effect=connect)
    monkeypatch.setattr(webdriver, "Remote", remote)

    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, Mock())
    desktop.start()
    signature.assert_not_called()
    caps = remote.call_args.kwargs["options"].to_capabilities()
    assert caps["appium:automationName"] == "Mac2"
    assert caps["appium:systemPort"] == 50120
    assert caps["appium:bundleId"] == "com.example.desktop"
    assert caps["appium:noReset"] is True
    assert caps["appium:skipAppKill"] is expected_skip_kill
    if kind == "installed":
        assert "appium:appPath" not in caps
        target = {"bundleId": "com.example.desktop"}
        assert launch.call_args.args[0] == ["/usr/bin/open", "-b", "com.example.desktop"]
    else:
        assert caps["appium:appPath"] == str(app.resolve())
        target = {"path": str(app.resolve())}
        if kind == "dmg":
            launch.assert_not_called()
            checked.assert_not_called()
        elif kind == "electron":
            assert launch.call_args.args[0] == ["/usr/bin/open", "-a", str(app.resolve()), "--args", "--force-renderer-accessibility"]
            assert caps["appium:arguments"] == ["--force-renderer-accessibility"]
        else:
            assert launch.call_args.args[0] == ["/usr/bin/open", "-a", str(app.resolve())]

    if kind != "dmg":
        checked.assert_called_once_with(build, 60)
        assert "ELECTRON_RUN_AS_NODE" not in launch.call_args.kwargs["env"]
        assert device.os.environ["ELECTRON_RUN_AS_NODE"] == "1"
    assert desktop.app_state() == 4
    driver.execute_script.assert_called_with("macos: queryAppState", target)
    desktop.activate()
    driver.execute_script.assert_called_with("macos: activateApp", target)
    desktop.stop()
    driver.quit.assert_called_once()
    device._stop_macos_runner.assert_called_once_with(50120)


def test_missing_mac2_driver_suggests_compatible_install(tmp_path, monkeypatch):
    build = resolve_build("com.example.desktop", "macos")
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock())
    monkeypatch.setattr(webdriver, "Remote", Mock(side_effect=RuntimeError(
        "Could not find a driver for automationName 'Mac2' and platformName 'mac'"
    )))
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, Mock())

    with pytest.raises(RuntimeError, match="appium driver install mac2@4.6.1"):
        desktop.start()


@pytest.mark.parametrize("message", ["xcodebuild exited with code 65", "'GET /status' cannot be proxied to Mac2 Driver server because its process is not running"])
def test_mac2_xcode_startup_error_emits_appium_setup_guidance(tmp_path, monkeypatch, message):
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock())
    original = RuntimeError(message)
    monkeypatch.setattr(webdriver, "Remote", Mock(side_effect=original))
    events = Mock()
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build("com.example.desktop", "macos"), tmp_path, events)

    with pytest.raises(RuntimeError, match="macOS automation prompt") as error:
        desktop.start()

    assert error.value.__cause__ is original
    events.assert_called_with("explore", str(error.value), level="warn")


def test_mac2_attach_timeout_distinguishes_launch_from_automation(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock())
    original = RuntimeError("Could not proxy command to the remote server. Original error: timeout of 240000ms exceeded")
    monkeypatch.setattr(webdriver, "Remote", Mock(side_effect=original))
    events = Mock()
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build("com.example.desktop", "macos"), tmp_path, events)

    with pytest.raises(RuntimeError, match="Mac2 timed out attaching to com.example.desktop") as error:
        desktop.start()

    assert error.value.__cause__ is original
    events.assert_called_with("explore", str(error.value), level="warn")


def test_mac2_ui_authorization_failure_explains_how_to_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock())
    original = RuntimeError("Failed to launch: Not authorized for performing UI testing actions.")
    monkeypatch.setattr(webdriver, "Remote", Mock(side_effect=original))
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build("com.example.desktop", "macos"), tmp_path, Mock())

    with pytest.raises(RuntimeError, match="macOS denied XCTest access.*Enable Automation") as error:
        desktop.start()

    assert error.value.__cause__ is original


def test_local_launch_failure_reports_the_selected_copy(tmp_path, monkeypatch):
    app = write_bundle(tmp_path / "Test Desktop.app")
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(device, "_has_invalid_macos_signature", Mock(return_value=False))
    monkeypatch.setattr(device.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "open", stderr="Application is unavailable")))
    appium = Mock()
    monkeypatch.setattr(device, "ensure_appium", appium)
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build(str(app), "macos"), tmp_path, Mock())

    with pytest.raises(RuntimeError, match=r"could not open .*Test Desktop\.app.*Application is unavailable"):
        desktop.start()

    appium.assert_not_called()


@pytest.mark.parametrize(("status", "message"), [
    ("another-copy", "A different copy"),
    ("exited", "exited during startup"),
    ("missing", "did not finish starting"),
    ("background", "started in the background"),
])
def test_mac2_starts_only_after_mac_app_launch_is_verified(tmp_path, monkeypatch, status, message):
    app = write_bundle(tmp_path / "Desktop Client.app")
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(device, "_has_invalid_macos_signature", Mock(return_value=False))
    monkeypatch.setattr(device.subprocess, "run", Mock())
    monkeypatch.setattr(device, "_macos_launch_status", Mock(return_value=status))
    appium = Mock()
    monkeypatch.setattr(device, "ensure_appium", appium)
    events = Mock()
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build(str(app), "macos"), tmp_path, events)

    with pytest.raises(RuntimeError, match=message):
        desktop.start()

    events.assert_called_once_with("launch", "Opening Test Desktop on the Mac desktop")
    appium.assert_not_called()


def test_failed_mac_launch_reports_an_invalid_signature_for_the_selected_bundle(tmp_path, monkeypatch):
    app = write_bundle(tmp_path / "Broken Desktop.app")
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    appium = Mock()
    monkeypatch.setattr(device, "ensure_appium", appium)
    monkeypatch.setattr(device, "_macos_launch_status", Mock(return_value="exited"))
    launch = Mock(side_effect=[
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 1, stderr="invalid signature (code or signature have been modified)"),
    ])
    monkeypatch.setattr(device.subprocess, "run", launch)
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), resolve_build(str(app), "macos"), tmp_path, Mock())

    with pytest.raises(RuntimeError, match=r"Broken Desktop\.app could not start.*invalid code signature"):
        desktop.start()

    assert launch.call_args.args == (["/usr/bin/codesign", "--verify", "--strict", str(app.resolve())],)
    assert launch.call_count == 2
    appium.assert_not_called()


@pytest.mark.parametrize(("state", "command"), [(1, "macos: launchApp"), (3, "macos: activateApp")])
def test_remote_mac_waits_for_the_selected_app_foreground(tmp_path, monkeypatch, state, command, mac_runner_port):
    app = write_bundle(tmp_path / "Test Desktop.app")
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    launch = Mock()
    monkeypatch.setattr(device.subprocess, "run", launch)
    monkeypatch.setattr(device, "ensure_appium", Mock())
    remote = Mock()
    remote.return_value.execute_script.side_effect = [state, None, 4]
    monkeypatch.setattr(webdriver, "Remote", remote)
    config = Settings(artifacts_dir=tmp_path, appium_server_url="http://remote.example:4723")
    desktop = device.AppDevice(config, resolve_build(str(app), "macos"), tmp_path, Mock())

    desktop.start()

    launch.assert_not_called()
    mac_runner_port.assert_not_called()
    assert "appium:systemPort" not in remote.call_args.kwargs["options"].to_capabilities()
    target = {"path": str(app.resolve())}
    assert remote.return_value.execute_script.call_args_list == [call("macos: queryAppState", target), call(command, target), call("macos: queryAppState", target)]


def test_repeated_local_mac_runs_request_different_xctest_runner_ports(tmp_path, monkeypatch, mac_runner_port):
    monkeypatch.setattr(device, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock(return_value=True))
    remote = Mock()
    remote.return_value.execute_script.return_value = 4
    monkeypatch.setattr(webdriver, "Remote", remote)
    settings = Settings(artifacts_dir=tmp_path)
    build = resolve_build("com.example.desktop", "macos")

    for _ in range(2):
        desktop = device.AppDevice(settings, build, tmp_path, Mock())
        desktop.start()
        desktop.stop()

    ports = [call.kwargs["options"].to_capabilities()["appium:systemPort"] for call in remote.call_args_list]
    assert ports == [50120, 50121]
    assert mac_runner_port.call_count == 2
    assert device._stop_macos_runner.call_args_list == [call(50120), call(50121)]


def test_mac2_does_not_switch_installations_after_selected_path_fails(tmp_path, monkeypatch):
    app = write_bundle(tmp_path / "Test Desktop.app")
    build = resolve_build(str(app), "macos")
    monkeypatch.setattr(device, "ensure_appium", Mock())
    monkeypatch.setattr(device.AppDevice, "_open_local_macos_app", Mock())
    remote = Mock(side_effect=RuntimeError(f"Failed to launch '{app}' application"))
    monkeypatch.setattr(webdriver, "Remote", remote)
    desktop = device.AppDevice(Settings(artifacts_dir=tmp_path), build, tmp_path, Mock())

    with pytest.raises(RuntimeError, match=r"selected app .*Test Desktop\.app"):
        desktop.start()

    remote.assert_called_once()


@pytest.mark.parametrize("mounted, invalid", [(True, False), (False, False), (False, True)])
def test_mac_build_cleans_up_only_disk_images_it_attaches(tmp_path, monkeypatch, mounted, invalid):
    image = tmp_path / "Desktop Build.dmg"
    image.write_bytes(b"disk image")
    volume = tmp_path / "Desktop Build"
    if invalid:
        volume.mkdir()
    else:
        app = write_bundle(volume / "Test Desktop.app")
    entities = [{"dev-entry": "/dev/disk99"}, {"dev-entry": "/dev/disk99s1", "mount-point": str(volume)}]
    calls = []

    def hdiutil(args, **kwargs):
        calls.append(args)
        if args[1] == "info":
            images = [{"image-path": str(image), "system-entities": entities}] if mounted else []
            return SimpleNamespace(stdout=plistlib.dumps({"images": images}))
        if args[1] == "attach":
            assert "-readonly" in args
            return SimpleNamespace(stdout=plistlib.dumps({"system-entities": entities}))
        assert args == ["hdiutil", "detach", "/dev/disk99"]
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(builds, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(builds.subprocess, "run", hdiutil)
    if invalid:
        with pytest.raises(RuntimeError, match="Expected exactly one"):
            resolve_build(str(image), "macos")
    else:
        build = resolve_build(str(image), "macos")
        assert build.launch_path == str(app)
        assert build.owns_mount is not mounted
        build.close()
        build.close()

    assert [args[1] for args in calls] == (["info"] if mounted else ["info", "attach", "detach"])


def test_macos_snapshot_excludes_hidden_text_and_apple_menu():
    snap = device.parse_mac("""
        <XCUIElementTypeApplication>
          <XCUIElementTypeWindow width="640" height="480">
            <XCUIElementTypeButton width="100" height="30">
              <XCUIElementTypeOther visible="false">
                <XCUIElementTypeStaticText value="Hidden label"/>
              </XCUIElementTypeOther>
              <XCUIElementTypeStaticText value="Visible label"/>
            </XCUIElementTypeButton>
          </XCUIElementTypeWindow>
          <XCUIElementTypeMenuBar>
            <XCUIElementTypeMenuBarItem title="Apple menu">
              <XCUIElementTypeMenuItem title="Restart…"/>
            </XCUIElementTypeMenuBarItem>
            <XCUIElementTypeMenuBarItem title="File"/>
          </XCUIElementTypeMenuBar>
        </XCUIElementTypeApplication>
    """)
    assert snap.texts == ["Visible label"]
    assert [el.name for el in snap.elements] == ["Visible label", "File"]


def test_macos_snapshot_tracks_windows_selection_scroll_and_submenus():
    snap = device.parse_mac("""
      <XCUIElementTypeApplication>
        <XCUIElementTypeWindow title="Main window" x="100" y="200" width="600" height="400">
          <XCUIElementTypeScrollView width="400" height="300">
            <XCUIElementTypeRadioButton title="Sidebar" selected="true" width="80" height="24"/>
            <XCUIElementTypeDisclosureTriangle title="Options" width="50" height="22"/>
          </XCUIElementTypeScrollView>
        </XCUIElementTypeWindow>
        <XCUIElementTypeWindow title="Second window" x="500" y="540" width="160" height="200">
          <XCUIElementTypeSwitch title="Unknown state" width="80" height="24"/>
        </XCUIElementTypeWindow>
        <XCUIElementTypeMenuBar>
          <XCUIElementTypeMenuBarItem title="View" width="20" height="20">
            <XCUIElementTypeMenu>
              <XCUIElementTypeMenuItem title="Panel" width="20" height="20">
                <XCUIElementTypeMenu visible="false" />
              </XCUIElementTypeMenuItem>
            </XCUIElementTypeMenu>
          </XCUIElementTypeMenuBarItem>
        </XCUIElementTypeMenuBar>
      </XCUIElementTypeApplication>
    """)
    sidebar, disclosure, toggle, view, panel = snap.elements
    assert snap.activity == "Main window"
    assert sidebar.checked is True and toggle.checked is None
    assert disclosure.role == "button"
    assert snap.element_windows[sidebar.path_hint] == (100, 200, 600, 400)
    assert snap.element_windows[toggle.path_hint] == (500, 540, 160, 200)
    assert snap.scrollable_elements == {sidebar.path_hint, disclosure.path_hint}
    assert [el.path_hint for el in snap.scroll_areas] == ["/Application[1]/Window[1]/ScrollView[1]"]
    assert panel.haspopup == "menu" and panel.path_hint.startswith(f"{view.path_hint}/Menu[1]/")


def test_desktop_scroll_targets_a_single_accessible_container(tmp_path):
    desktop = device.AppDevice(Settings(), BuildInfo("macos", "installed", app_id="com.example.desktop"), tmp_path, Mock())
    desktop.driver = Mock()
    container = ObservedElement(selector="bounds:3,4,50,100", role="scrollarea", name="", tag="ScrollView", bbox=[3, 4, 50, 100])
    matching = Mock(id="match")
    matching.rect = {"x": 3, "y": 4, "width": 50, "height": 100}
    matching.is_displayed.return_value = True
    desktop.driver.find_elements.return_value = [matching]

    assert desktop.scroll_area(container, -240)
    desktop.driver.find_elements.assert_called_once_with("class name", "XCUIElementTypeScrollView")
    desktop.driver.execute_script.assert_called_once_with("macos: scroll", {"elementId": "match", "deltaX": 0, "deltaY": -240})

    desktop.driver.execute_script.reset_mock()
    desktop.driver.find_elements.return_value = [matching, matching]
    assert not desktop.scroll_area(container, -240)
    desktop.driver.execute_script.assert_not_called()


def test_strict_mac_field_value_distinguishes_empty_from_unreadable(tmp_path):
    desktop = device.AppDevice(Settings(), BuildInfo("macos", "installed", app_id="com.example.desktop"), tmp_path, Mock())
    desktop.driver = Mock()
    field = ObservedElement(selector="~search", role="searchbox", name="Search", tag="SearchField")
    target = Mock()
    desktop.driver.find_elements.return_value = [target]
    target.get_attribute.return_value = ""
    assert desktop.read_value(field, strict=True) == ""

    target.get_attribute.return_value = None
    assert desktop.read_value(field) == ""
    with pytest.raises(RuntimeError, match="readable text value"):
        desktop.read_value(field, strict=True)


def test_macos_lookup_uses_type_and_never_guesses_a_coordinate(tmp_path):
    desktop = device.AppDevice(Settings(), BuildInfo("macos", "installed", app_id="com.example.desktop"), tmp_path, Mock())
    desktop.driver = Mock()
    desktop.driver.find_elements.return_value = []
    element = ObservedElement(selector='title:Open "Notes"', role="button", tag="Button", name='Open "Notes"', bbox=[5, 10, 20, 20])

    assert not desktop.tap(element)
    strategy, predicate = desktop.driver.find_elements.call_args.args
    assert strategy == "predicate string" and 'amType == "XCUIElementTypeButton"' in predicate
    assert '"Open \\"Notes\\""' in predicate
    assert not desktop.type_into(element, "fixture text")
    desktop.driver.execute_script.assert_not_called()


def test_macos_screenshot_uses_visible_app_window_only(tmp_path):
    desktop = device.AppDevice(Settings(), BuildInfo("macos", "installed", app_id="com.example.desktop"), tmp_path, Mock())
    desktop.driver = Mock()
    hidden = Mock()
    hidden.is_displayed.return_value = False
    window = Mock()
    window.is_displayed.return_value = True
    window.screenshot.side_effect = lambda path: Path(path).write_bytes(b"window only") > 0
    desktop.driver.find_elements.return_value = [hidden, window]

    assert Path(desktop.screenshot("visible window")).read_bytes() == b"window only"
    desktop.driver.find_elements.return_value = [hidden]
    assert desktop.screenshot("no visible window") == ""
    hidden.screenshot.assert_not_called()
    desktop.driver.get_screenshot_as_file.assert_not_called()


def test_macos_explorer_labels_mutating_menus_and_missing_foreground():
    dev = Mock()
    dev.platform = "macos"
    dev.snapshot.return_value = device.Snapshot()
    dev.app_state.return_value = 3
    dev.errors_since_last.return_value = []
    explorer = AppExplorer(dev, Mock())
    item = ObservedElement(selector="title:Duplicate", role="menuitem", tag="MenuItem", name="Duplicate")

    assert explorer.classify(item) == "mutating"
    assert explorer.classify(ObservedElement(selector="title:Services", role="menuitem", tag="MenuItem", name="Services")) == "external"
    tap = Tap(screen_sig="screen", element=item)
    explorer.observe_after(Screen(sig="screen", snap=device.Snapshot()), tap)
    assert tap.result == "foreign"


def test_macos_active_modules_do_not_try_mobile_or_restart_checks():
    dev = Mock()
    dev.platform = "macos"
    explorer = AppExplorer(dev, Mock())
    events = Mock()
    checks = ActiveChecks(dev, explorer, events, "home")
    checks.run({"launch", "relaunch", "offline", "perf", "rotation", "resume"})
    assert [(test.technique, test.status) for test in checks.tests] == [("launch", "blocked"), ("relaunch", "blocked")]
    assert all("preserve the existing app session" in test.observed for test in checks.tests)
    dev.snapshot.assert_not_called()
    dev.relaunch.assert_not_called()
    events.assert_not_called()
