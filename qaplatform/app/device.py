"""One Appium session on the device under test, and what we read from it.

The explorer and the checks only use this module to touch the app: launch,
snapshot (UI tree -> ObservedElement list), tap, type, back, background,
network on/off, and the health signals that decide whether an action broke
something (app no longer in the foreground, crash or ANR dialog, fatal or JS
errors in the device log).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from qaplatform.app.build import BuildInfo
from qaplatform.config import Settings
from qaplatform.explore.crawler import ObservedElement

logger = logging.getLogger(__name__)

ANDROID_ROLE = {
    "android.widget.Button": "button", "android.widget.ImageButton": "button", "android.widget.EditText": "textbox",
    "android.widget.AutoCompleteTextView": "textbox", "android.widget.Switch": "switch", "android.widget.ToggleButton": "switch",
    "android.widget.CheckBox": "checkbox", "android.widget.RadioButton": "radio", "android.widget.Spinner": "combobox",
    "android.widget.SeekBar": "slider", "android.widget.TabWidget": "tab",
}
MAC_ROLE = {
    "XCUIElementTypeButton": "button", "XCUIElementTypeMenuBarItem": "menuitem", "XCUIElementTypeMenuItem": "menuitem", "XCUIElementTypeLink": "link",
    "XCUIElementTypeTextField": "textbox", "XCUIElementTypeSecureTextField": "textbox", "XCUIElementTypeTextView": "textbox", "XCUIElementTypeSearchField": "searchbox",
    "XCUIElementTypeCheckBox": "checkbox", "XCUIElementTypeRadioButton": "radio", "XCUIElementTypePopUpButton": "combobox", "XCUIElementTypeComboBox": "combobox",
    "XCUIElementTypeSwitch": "switch", "XCUIElementTypeTab": "tab", "XCUIElementTypeMenuButton": "button", "XCUIElementTypeSlider": "slider",
}
TEXT_CLASSES = ("android.widget.TextView", "XCUIElementTypeStaticText")
CRASH_DIALOG_RE = re.compile(r"(keeps stopping|has stopped|isn.t responding|unfortunately,? .* has stopped)", re.I)
FATAL_LOG_RE = re.compile(r"(FATAL EXCEPTION|AndroidRuntime.*(Exception|Error)|ANR in |ReactNativeJS.*(Error|Exception|Unhandled|TypeError|undefined is not)|Fatal signal \d+|Process .* has died)", re.I)
SYSTEM_PACKAGES = ("com.android.systemui", "com.google.android.permissioncontroller", "com.android.permissioncontroller", "com.google.android.inputmethod", "com.android.packageinstaller")
APPLE_MENU_LABELS = {"apple", "apple menu", ""}
MAC_SCROLL_AREAS = {"ScrollView", "List", "Table", "Outline", "CollectionView"}
ELECTRON_ACCESSIBILITY_ARG = "--force-renderer-accessibility"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
APK_INSTALL_TIMEOUT_S = 180
MACOS_LAUNCH_STATUS_SCRIPT = """
ObjC.import("AppKit");

function run(argv) {
    const [bundleId, path, timeout] = argv;
    const selected = path && ObjC.unwrap($.NSURL.fileURLWithPath(path).URLByResolvingSymlinksInPath.path);
    const deadline = Date.now() + Number(timeout) * 1000;
    let sawApp = false;
    let status = "missing";
    do {
        const apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(bundleId);
        let matched = false;
        status = "missing";
        for (let index = 0; index < apps.count; index++) {
            const app = apps.objectAtIndex(index);
            if (app.terminated) continue;
            const location = ObjC.unwrap(app.bundleURL.URLByResolvingSymlinksInPath.path);
            if (selected && location !== selected) {
                if (!matched) status = "another-copy";
                continue;
            }
            matched = true;
            sawApp = true;
            if (app.finishedLaunching) {
                if (app.active) return "foreground";
                if (Number(app.activationPolicy) !== 0) return "accessory";
                status = "background";
            } else if (status !== "background") {
                status = "starting";
            }
        }
        if (sawApp && !matched) return "exited";
        $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.2));
    } while (Date.now() < deadline);
    return status;
}
"""


@dataclass
class Snapshot:
    """What is on screen right now."""
    package: str = ""
    activity: str = ""
    elements: list[ObservedElement] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    origin: tuple[int, int] = (0, 0)  # top-left of the window in screen coordinates (macOS)
    system_dialog: str = ""  # text of a crash / ANR dialog if one is showing
    foreign: bool = False  # another app (camera, browser, permission prompt) is in front
    xml: str = ""
    scroll_areas: list[ObservedElement] = field(default_factory=list)
    element_windows: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    scrollable_elements: set[str] = field(default_factory=set)

    @property
    def signature(self) -> str:
        """Identity of the screen: what can be done on it, not the data shown."""
        names = sorted({f"{e.role}:{e.name}" for e in self.elements if e.visible})[:60]
        return hashlib.sha1("|".join([self.activity, *names]).encode()).hexdigest()[:12]

    @property
    def title(self) -> str:
        """A heading-like text: not a control's own label, more than a logo or initials."""
        labels = {e.name for e in self.elements}
        candidates = [t for t in self.texts[:12] if 2 < len(t) < 60 and t not in labels]
        for t in candidates:
            if " " in t.strip() or len(t) >= 8:
                return t
        return candidates[0] if candidates else (self.activity or "screen")


def _bounds(raw: str) -> list[int]:
    nums = [int(n) for n in re.findall(r"-?\d+", raw or "")]
    if len(nums) == 4:
        x1, y1, x2, y2 = nums
        return [x1, y1, max(0, x2 - x1), max(0, y2 - y1)]
    return []


def _descendant_texts(node: ET.Element, limit: int = 3) -> list[str]:
    out = []
    pending = list(reversed(node))
    while pending and len(out) < limit:
        d = pending.pop()
        if d.get("displayed", "true") == "false" or d.get("visible", "true") == "false":
            continue
        t = (d.get("text") or d.get("value") or d.get("label") or "").strip()
        if t and t not in out:
            out.append(t)
        pending.extend(reversed(d))
    return out


def parse_android(xml: str, package: str) -> Snapshot:
    snap = Snapshot(xml=xml)
    root = ET.fromstring(xml)
    snap.width, snap.height = int(root.get("width") or 0), int(root.get("height") or 0)
    seen_ids: dict[str, int] = {}
    for idx, node in enumerate(root.iter()):
        cls = node.get("class") or node.tag
        pkg = node.get("package") or ""
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        visible = node.get("displayed", "true") == "true"
        if pkg and pkg != package and CRASH_DIALOG_RE.search(text):
            snap.system_dialog = text
        if visible and text and pkg in ("", package) and cls.endswith("TextView"):
            snap.texts.append(text)
        clickable = node.get("clickable") == "true" or node.get("checkable") == "true" or cls in ANDROID_ROLE
        if not clickable or (pkg and pkg != package):
            continue
        bbox = _bounds(node.get("bounds", ""))
        if bbox and (bbox[2] == 0 or bbox[3] == 0):
            continue
        role = ANDROID_ROLE.get(cls, "button")
        rid = node.get("resource-id") or ""
        name = desc or text or (node.get("hint") or "") or " ".join(_descendant_texts(node, 1))
        if rid:
            seen_ids[rid] = seen_ids.get(rid, 0) + 1
        if desc:
            selector = f"~{desc}"
        elif rid and not rid.startswith("android:id/"):
            selector = f"id:{rid}"
        elif text:
            selector = f'xpath://*[@text="{text}"]'
        else:
            selector = f"bounds:{','.join(map(str, bbox))}"
        snap.elements.append(ObservedElement(
            selector=selector, role=role, name=name[:200], tag=cls.rsplit(".", 1)[-1], type="password" if node.get("password") == "true" else None,
            id=rid.split("/", 1)[-1] if rid else None, testid=rid.split("/", 1)[-1] if rid else None, visible=visible,
            enabled=node.get("enabled", "true") == "true", checked=(node.get("checked") == "true") if node.get("checkable") == "true" else None,
            bbox=bbox, path_hint=str(idx),
        ))
    return snap


def parse_mac(xml: str, *, desktop: bool = True) -> Snapshot:
    snap = Snapshot(xml=xml)
    root = ET.fromstring(xml)
    idx = 0

    def visit(node: ET.Element, in_dialog: bool, parent_visible: bool, path: str, window: tuple | None, scrollable: bool) -> None:
        nonlocal idx
        idx += 1
        cls = node.tag
        tag = cls.removeprefix("XCUIElementType")
        label = (node.get("title") or node.get("label") or "").strip()
        value = (node.get("value") or "").strip()
        visible = parent_visible and node.get("visible", "true").lower() not in ("false", "0")
        enabled = node.get("enabled", "true").lower() in ("true", "1")
        try:
            bbox = [int(float(node.get(k) or 0)) for k in ("x", "y", "width", "height")]
        except (ValueError, OverflowError):
            bbox = []
        # The system Apple menu (Sleep, Restart, Shut Down...) is part of every app's menu bar: never collect it.
        if cls == "XCUIElementTypeMenuBarItem" and label.casefold() in APPLE_MENU_LABELS:
            return
        if tag == "Window" and visible and bbox and bbox[2] > 0 and bbox[3] > 0:
            window = tuple(bbox)
            if not snap.width:
                x, y, snap.width, snap.height = window
                snap.origin = (x, y)
                if desktop:
                    snap.activity = label
        dialog = in_dialog or cls in ("XCUIElementTypeSheet", "XCUIElementTypeDialog")
        if visible and cls in TEXT_CLASSES and (value or label):
            snap.texts.append((value or label)[:200])
        role = MAC_ROLE.get(cls)
        if desktop and tag == "DisclosureTriangle":
            role = "button"
        scroll_area = desktop and tag in MAC_SCROLL_AREAS
        if role or scroll_area:
            name = label or (node.get("placeholderValue") or "")
            if role and not name:
                name = " ".join(_descendant_texts(node, 1))
            ident = node.get("identifier") or ""
            if ident and not ident.endswith(":"):
                selector = f"~{ident}"
            elif label:
                selector = f"title:{label}"
            else:
                selector = f"bounds:{','.join(map(str, bbox))}"
            checked = None
            if role in ("checkbox", "switch", "radio", "tab"):
                selection = node.get("selected") if role in ("radio", "tab") else None
                state = (selection if selection is not None else value or node.get("checked", "")).lower()
                if state in ("0", "1", "true", "false", "on", "off"):
                    checked = state in ("1", "true", "on")
            submenu = desktop and tag == "MenuItem" and any(child.tag == "XCUIElementTypeMenu" for child in node)
            element = ObservedElement(
                selector=selector, role=role or "scrollarea", name=name[:200], tag=tag, type="password" if cls == "XCUIElementTypeSecureTextField" else None,
                id=ident or None, testid=ident or None, visible=visible and (not bbox or (bbox[2] > 0 and bbox[3] > 0)),
                enabled=enabled, checked=checked, bbox=bbox, path_hint=path if desktop else str(idx),
                in_dialog=dialog, haspopup="menu" if submenu else None,
            )
            if desktop and window:
                snap.element_windows[path] = window
            if role:
                snap.elements.append(element)
                if desktop and scrollable:
                    snap.scrollable_elements.add(path)
            if scroll_area and element.visible and element.enabled:
                snap.scroll_areas.append(element)
        child_counts: dict[str, int] = {}
        for child in node:
            child_tag = child.tag.removeprefix("XCUIElementType")
            child_counts[child_tag] = child_counts.get(child_tag, 0) + 1
            visit(child, dialog, visible, f"{path}/{child_tag}[{child_counts[child_tag]}]", window, scrollable or scroll_area)

    visit(root, False, True, f"/{root.tag.removeprefix('XCUIElementType')}[1]", None, False)
    return snap


def _appium_up(url: str) -> bool:
    try:
        with urlopen(f"{url}/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _gui_environment() -> dict[str, str]:
    # Electron-based editors can set this for their CLI. A GUI app must start as Electron.
    environment = os.environ.copy()
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    return environment


def _has_invalid_macos_signature(path: str) -> bool:
    if not path:
        return False
    try:
        result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", path], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False  # Launch Services still applies the operating system's normal checks.
    details = result.stderr.casefold()
    return bool(result.returncode) and any(message in details for message in (
        "invalid signature", "code or signature have been modified", "a sealed resource is missing or invalid",
    ))


def _macos_launch_status(build: BuildInfo, timeout: int) -> str:
    command = ["/usr/bin/osascript", "-l", "JavaScript", "-e", MACOS_LAUNCH_STATUS_SCRIPT, build.app_id, build.launch_path, str(timeout)]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout + 5)
    return result.stdout.strip()


def _macos_runner_port() -> int:
    """Pick a free port so Mac2 starts a fresh XCTest process for this run."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]
    except OSError as exc:
        raise RuntimeError("Could not allocate a local port for the macOS test runner.") from exc


def _stop_macos_runner(port: int) -> None:
    """End the XCTest process on the private port assigned to this run."""
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/status")
        with connection.getresponse() as response:
            if response.status != 200:
                return
            status = json.loads(response.read(8192)).get("value", {})
        if status.get("message") != "WebDriverAgent is ready to accept commands":
            logger.warning("The listener on port %s did not identify itself as WebDriverAgent", port)
            return
        connection.request("DELETE", "/")
        with connection.getresponse() as response:
            if response.status != 200:
                logger.warning("The Mac2 runner rejected shutdown with HTTP %s", response.status)
                return
            response.read(128)
    except ConnectionRefusedError:
        return  # The fresh runner already exited.
    except (OSError, HTTPException, ValueError, AttributeError):
        logger.warning("Could not shut down the Mac2 runner for this session", exc_info=True)
        return
    finally:
        connection.close()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.settimeout(0.25)
            if listener.connect_ex(("127.0.0.1", port)):
                logger.info("Mac2 runner exited and released its XCTest session")
                return
        time.sleep(0.15)
    logger.warning("The Mac2 runner still listens on port %s after shutdown", port)


def ensure_appium(settings: Settings, log_dir: Path) -> None:
    """Start a local Appium server when none answers and the URL is local."""
    url = settings.appium_server_url
    if _appium_up(url):
        return
    host = urlparse(url).hostname or ""
    if not settings.appium_autostart or host not in LOOPBACK_HOSTS or not shutil.which("appium"):
        raise RuntimeError(f"Appium server not reachable at {url}. Start it with `appium` (and `appium driver install uiautomator2` / `mac2`), "
                           "or set APPIUM_SERVER_URL")
    log_dir.mkdir(parents=True, exist_ok=True)
    port = str(urlparse(url).port or 4723)
    with open(log_dir / "appium.log", "ab") as fh:
        subprocess.Popen(["appium", "--address", host, "--port", port, "--log-level", "warn"], stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True, env=_gui_environment())
    for _ in range(40):
        if _appium_up(url):
            return
        time.sleep(1)
    raise RuntimeError(f"Started Appium but it did not answer at {url}; see {log_dir / 'appium.log'}")


def _android_sdk_dirs() -> list[Path]:
    configured = [Path(value).expanduser() for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT") if (value := os.environ.get(key))]
    return [*configured, Path.home() / "Library/Android/sdk"]


def _adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    for sdk in _android_sdk_dirs():
        candidate = sdk / "platform-tools" / "adb"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("adb not found: install Android SDK platform-tools")


def ensure_android_device(settings: Settings, log_dir: Path, events) -> str:
    """Return the serial of a booted device, starting an emulator if none is connected."""
    adb = _adb()

    def ready() -> list[str]:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20).stdout
        return [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]

    serials = ready()
    if settings.device_name:
        if settings.device_name not in serials:
            raise RuntimeError(f"Android device {settings.device_name} (DEVICE_NAME) is not connected and ready")
        return settings.device_name
    if serials:
        return serials[0]
    sdk_dirs = [*_android_sdk_dirs(), Path(adb).resolve().parent.parent]
    candidates = (sdk / "emulator" / "emulator" for sdk in sdk_dirs)
    emulator = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("emulator")
    if not emulator:
        raise RuntimeError("No Android device connected and the emulator binary was not found")
    avds = subprocess.run([emulator, "-list-avds"], capture_output=True, text=True, timeout=30).stdout.split()
    avd = settings.android_avd or (avds[0] if avds else "")
    if not avd:
        raise RuntimeError("No Android device connected and no AVD configured (create one in Android Studio, or set ANDROID_AVD)")
    events("launch", f"Starting Android emulator {avd}")
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "emulator.log", "ab") as fh:
        subprocess.Popen([emulator, "-avd", avd, "-no-snapshot-save"], stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(120):
        serials = [serial for serial in ready() if serial.startswith("emulator-")]
        if serials:
            boot = subprocess.run([adb, "-s", serials[0], "shell", "getprop", "sys.boot_completed"], capture_output=True, text=True, timeout=20).stdout.strip()
            if boot == "1":
                return serials[0]
        time.sleep(2)
    raise RuntimeError(f"Emulator {avd} did not boot; see {log_dir / 'emulator.log'}")


class AppDevice:
    """A live Appium session on the app under test."""

    def __init__(self, settings: Settings, build: BuildInfo, run_dir: Path, events, clean_start: bool = False):
        self.settings = settings
        self.build = build
        self.run_dir = run_dir
        self.events = events
        self.clean_start = clean_start
        self.platform = build.platform
        self.driver = None
        self.serial = ""
        self._shot = 0
        self._runner_port: int | None = None

    # -- session ---------------------------------------------------------
    def start(self) -> None:
        from appium import webdriver
        from appium.options.common import AppiumOptions

        b = self.build
        macos_opened = False
        if self.platform == "macos":
            if not b.app_id:
                raise ValueError("Choose an installed macOS app or a bundle with a valid identifier")
            macos_opened = self._open_local_macos_app()
        log_dir = self.settings.artifacts_dir / "logs"
        ensure_appium(self.settings, log_dir)
        o = AppiumOptions()
        o.set_capability("appium:newCommandTimeout", 300)
        if self.platform == "android":
            self.serial = ensure_android_device(self.settings, log_dir, self.events)
            o.set_capability("platformName", "Android")
            o.set_capability("appium:automationName", "UiAutomator2")
            o.set_capability("appium:udid", self.serial)
            o.set_capability("appium:autoGrantPermissions", True)
            o.set_capability("appium:noReset", not self.clean_start)
            o.set_capability("appium:fullReset", False)
            o.set_capability("appium:noSign", True)
            if b.path:
                # Appium can skip installation for an installed package when noReset is enabled.
                self.events("install", f"Installing APK on Android device {self.serial}")
                try:
                    result = subprocess.run([_adb(), "-s", self.serial, "install", "-r", b.path], capture_output=True, text=True, timeout=APK_INSTALL_TIMEOUT_S)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"APK installation on Android device {self.serial} timed out after {APK_INSTALL_TIMEOUT_S} seconds") from exc
                if result.returncode or "Success" not in result.stdout.splitlines():
                    detail = (result.stderr.strip() or result.stdout.strip()).replace(b.path, Path(b.path).name)
                    raise RuntimeError(f"APK installation failed on Android device {self.serial}: {detail[:500] or 'ADB did not confirm installation'}")
                self.events("install", f"APK installed on Android device {self.serial}")
                if not b.app_id or not b.activity:
                    o.set_capability("appium:app", b.path)  # let Appium read missing manifest metadata
            if b.app_id:
                o.set_capability("appium:appPackage", b.app_id)
            if b.activity:
                o.set_capability("appium:appActivity", b.activity)
                o.set_capability("appium:appWaitActivity", "*")
            o.set_capability("appium:appWaitDuration", self.settings.app_launch_timeout_s * 1000)
        elif self.platform == "ios":
            o.set_capability("platformName", "iOS")
            o.set_capability("appium:automationName", "XCUITest")
            if self.settings.device_name:
                o.set_capability("appium:deviceName", self.settings.device_name)
            o.set_capability("appium:noReset", not self.clean_start)
            if b.path:
                o.set_capability("appium:app", b.path)
            elif b.app_id:
                o.set_capability("appium:bundleId", b.app_id)
        elif self.platform == "macos":
            o.set_capability("platformName", "mac")
            o.set_capability("appium:automationName", "Mac2")
            if sys.platform == "darwin" and urlparse(self.settings.appium_server_url).hostname in LOOPBACK_HOSTS:
                # Mac2 keeps its XCTest process when these capabilities match. A previous
                # process can still answer HTTP after macOS has revoked UI testing access.
                self._runner_port = _macos_runner_port()
                o.set_capability("appium:systemPort", self._runner_port)
                self.events("explore", "Starting a fresh macOS test runner. Authenticate if macOS shows Enable Automation.")
            o.set_capability("appium:bundleId", b.app_id)
            if b.launch_path:
                o.set_capability("appium:appPath", b.launch_path)
            if arguments := self._mac_arguments():
                o.set_capability("appium:arguments", arguments)
            o.set_capability("appium:noReset", True)
            o.set_capability("appium:skipAppKill", not b.owns_mount)
            o.set_capability("appium:showServerLogs", False)
        else:
            raise ValueError(f"Unsupported app platform: {self.platform}")
        t0 = time.time()
        try:
            self.driver = webdriver.Remote(self.settings.appium_server_url, options=o)
        except Exception as exc:
            if self.platform == "macos":
                detail = str(exc).casefold()
                if "could not find a driver" in detail:
                    reason = ("The Appium server has no Mac2 driver. Install a compatible Mac2 driver and restart Appium. "
                              "For Appium 3, use `appium driver install mac2@4.6.1`.")
                elif "not authorized for performing ui testing" in detail or "automation mode is disabled" in detail:
                    reason = ("macOS denied XCTest access to the app. Keep the desktop unlocked, authenticate at the Enable Automation "
                              "prompt, and retry the run.")
                elif any(hint in detail for hint in ("xcodebuild", "automation mode", "cannot be proxied to mac2 driver server")):
                    reason = ("Mac2 could not start WebDriverAgent. Keep the Mac desktop unlocked and respond to any macOS automation prompt. "
                              "Check Accessibility permissions, run `appium driver doctor mac2`, and review the Xcode test log for the underlying error.")
                elif "could not proxy command" in detail and "timeout" in detail:
                    reason = (f"Mac2 timed out attaching to {b.label or b.app_id}. Keep the Mac unlocked and authenticate if macOS shows "
                              "an Enable Automation prompt. If the app is open and no prompt appears, check the Xcode test log for denied "
                              "UI testing access.")
                elif "failed to launch" in detail:
                    if macos_opened:
                        reason = (f"The selected app opened ({b.launch_path or b.app_id}). Mac2 could not attach to it. "
                                  "Check the Xcode test log and respond to any Enable Automation prompt.")
                    else:
                        reason = (f"Mac2 could not launch the selected app ({b.launch_path or b.app_id}). Open that copy from Finder to see "
                                  "any launch prompt. If another copy is open, save its work and quit it before trying the selected copy again.")
                else:
                    raise
                self.events("explore", reason, level="warn")
                raise RuntimeError(reason) from exc
            raise
        self.driver.implicitly_wait(0)
        if self.platform == "android":
            self.build.app_id = self.build.app_id or self.driver.current_package
            self._drain_log()
        elif self.platform == "macos":
            self._wait_for_macos_foreground()
        self.launch_seconds = round(time.time() - t0, 1)

    def _mac_arguments(self) -> list[str]:
        return [ELECTRON_ACCESSIBILITY_ARG] if self.build.framework == "electron" else []

    def _open_local_macos_app(self) -> bool:
        """Open the exact local target. Mac2 manages remote and run-mounted DMG apps."""
        b = self.build
        server_host = urlparse(self.settings.appium_server_url).hostname
        if sys.platform != "darwin" or server_host not in LOOPBACK_HOSTS or b.owns_mount:
            return False
        target = b.launch_path or b.app_id
        name = b.label or target
        command = ["/usr/bin/open", "-a" if b.launch_path else "-b", target]
        if arguments := self._mac_arguments():
            command.extend(["--args", *arguments])
        self.events("launch", f"Opening {name} on the Mac desktop")
        timeout = max(1, self.settings.app_launch_timeout_s)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=min(20, timeout), env=_gui_environment())
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()[:300]
            suffix = f" {detail}" if detail else ""
            raise self._macos_launch_error(f"macOS could not open {target}. Verify that the selected app opens from Finder.{suffix}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"macOS timed out opening {target}. Keep the desktop unlocked and respond to any launch prompt.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not ask macOS to open {target}. Start the worker from the Mac desktop session.") from exc
        try:
            status = _macos_launch_status(b, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"The worker could not verify that macOS opened {target}.") from exc
        if status == "another-copy":
            raise RuntimeError(f"A different copy of {b.app_id} is running. Select that copy in the picker, or quit it before opening {target}.")
        if status == "exited":
            raise self._macos_launch_error(f"{target} exited during startup. Check its macOS crash or startup log, or try another build.")
        if status == "background":
            raise RuntimeError(f"{target} started in the background. Keep the Mac unlocked and check for an app launch prompt.")
        if status not in ("foreground", "accessory"):
            raise self._macos_launch_error(f"{target} did not finish starting. Open it from Finder to check for a launch prompt or startup error.")
        location = "in the foreground" if status == "foreground" else "as a menu-bar or background app"
        self.events("launch", f"{name} opened {location}. Connecting Appium.")
        return True

    def _macos_launch_error(self, message: str) -> RuntimeError:
        path = self.build.launch_path
        if _has_invalid_macos_signature(path):
            return RuntimeError(f"{path} could not start. macOS reported an invalid code signature. Try another installed copy or a freshly signed build.")
        return RuntimeError(message)

    def _wait_for_macos_foreground(self) -> None:
        state = self.app_state()
        if state in (-1, 0, 4):
            return  # A server without the state endpoint can still return an app-scoped UI tree.
        target = self._mac_target()
        try:
            if state == 1:
                if arguments := self._mac_arguments():
                    target["arguments"] = arguments
                self.driver.execute_script("macos: launchApp", target)
            else:
                self.driver.execute_script("macos: activateApp", target)
        except Exception as exc:
            raise RuntimeError(f"Mac2 could not bring {self.build.label or self.build.app_id} to the foreground.") from exc
        end = time.monotonic() + max(0, self.settings.app_launch_timeout_s)
        while True:
            if self.app_state() == 4:
                return
            if time.monotonic() >= end:
                raise RuntimeError(f"The selected macOS app did not reach the foreground ({self.build.app_id}). Check for an app launch prompt.")
            time.sleep(0.25)

    def stop(self) -> None:
        try:
            if self.driver is not None:
                try:
                    self.set_network(True)
                except Exception:
                    pass
                self.driver.quit()
        except Exception:
            logger.warning("Could not close the Appium session", exc_info=True)
        finally:
            self.driver = None
            if self._runner_port is not None:
                port, self._runner_port = self._runner_port, None
                _stop_macos_runner(port)

    # -- reading ---------------------------------------------------------
    def snapshot(self, settle: float = 0.8) -> Snapshot:
        if settle:
            time.sleep(settle)
        xml = self.driver.page_source
        if self.platform == "android":
            snap = parse_android(xml, self.build.app_id)
            try:
                snap.package = self.driver.current_package or ""
                snap.activity = self.driver.current_activity or ""
            except Exception:
                pass
            snap.foreign = bool(snap.package) and snap.package != self.build.app_id
        else:
            snap = parse_mac(xml, desktop=self.platform == "macos")  # XCUITest (iOS) and Mac2 share the XCUIElementType tree
            snap.package = self.build.app_id
        return snap

    def app_state(self) -> int:
        """4 = foreground, 3 = background, 2 = suspended, 1 = not running, -1 = unknown."""
        if not self.build.app_id:
            return -1
        try:
            if self.platform == "macos":  # Mac2 only implements its own execute methods
                return int(self.driver.execute_script("macos: queryAppState", self._mac_target()))
            return int(self.driver.query_app_state(self.build.app_id))
        except Exception:
            return -1

    def _drain_log(self) -> list[dict]:
        if self.platform != "android":
            return []
        try:
            return self.driver.get_log("logcat")
        except Exception:
            return []

    def errors_since_last(self) -> list[str]:
        """Fatal / JS error lines logged since the previous call."""
        out = []
        for entry in self._drain_log():
            msg = str(entry.get("message", ""))
            if FATAL_LOG_RE.search(msg) and (self.build.app_id in msg or "ReactNativeJS" in msg or "AndroidRuntime" in msg or "FATAL" in msg):
                out.append(msg[:400])
        return out[:20]

    def toast(self) -> str:
        """Android toasts are not in the UI tree; UiAutomator2 exposes the last one through this XPath."""
        if self.platform != "android":
            return ""
        from appium.webdriver.common.appiumby import AppiumBy

        try:
            found = self.driver.find_elements(AppiumBy.XPATH, "//android.widget.Toast")
            return (found[0].text or "").strip() if found else ""
        except Exception:
            return ""

    def screenshot(self, tag: str) -> str:
        self._shot += 1
        shots = self.run_dir / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{self._shot:03d}_{re.sub(r'[^A-Za-z0-9]+', '_', tag)[:50]}.png"
        try:
            if self.platform == "macos":
                windows = self.driver.find_elements("class name", "XCUIElementTypeWindow")
                window = next((window for window in windows if window.is_displayed()), None)
                saved = window is not None and window.screenshot(str(path))
            else:
                saved = self.driver.get_screenshot_as_file(str(path))
        except Exception:
            return ""
        return str(path) if saved else ""

    # -- acting ----------------------------------------------------------
    def find(self, el: ObservedElement):
        from appium.webdriver.common.appiumby import AppiumBy

        sel = el.selector
        geometry_lookup = self.platform == "macos" and sel.startswith("bounds:") and len(el.bbox) == 4
        try:
            if sel.startswith("~"):
                found = self.driver.find_elements(AppiumBy.ACCESSIBILITY_ID, sel[1:])
            elif sel.startswith("id:"):
                found = self.driver.find_elements(AppiumBy.ID, sel[3:])
            elif sel.startswith("xpath:"):
                found = self.driver.find_elements(AppiumBy.XPATH, sel[6:])
            elif sel.startswith("title:"):
                if self.platform == "macos":
                    title = json.dumps(sel[6:], ensure_ascii=False)
                    kind = json.dumps(f"XCUIElementType{el.tag}")
                    found = self.driver.find_elements("predicate string", f"(title == {title} OR label == {title}) AND amType == {kind}")
                else:
                    title = sel[6:].replace("'", "\\'")
                    found = self.driver.find_elements(AppiumBy.IOS_PREDICATE, f"title == '{title}' OR label == '{title}'")
            elif geometry_lookup:
                # A type-and-frame lookup still produces an app-scoped element ID for the gesture.
                found = self.driver.find_elements("class name", f"XCUIElementType{el.tag}")
            else:
                found = []
            if self.platform == "macos" and (geometry_lookup or len(found) > 1):
                matches = []
                for match in found:
                    rect = match.rect
                    if [int(rect[key]) for key in ("x", "y", "width", "height")] == el.bbox and match.is_displayed():
                        matches.append(match)
                return matches[0] if len(matches) == 1 else None
        except Exception:
            found = []
        return found[0] if found else None

    def _mac_gesture(self, command: str, el: ObservedElement, **offsets: int) -> bool:
        if self.platform != "macos" or not el.visible or not el.enabled:
            return False
        target = self.find(el)
        if target is None:
            return False
        try:
            if not target.is_displayed() or not target.id:
                return False
            self.driver.execute_script(command, {"elementId": target.id, **offsets})
            return True
        except Exception:
            return False

    def hover_menu(self, el: ObservedElement) -> bool:
        """Reveal a submenu using its accessible app-menu element."""
        if el.tag != "MenuItem":
            return False
        return self._mac_gesture("macos: hover", el)

    def scroll_area(self, el: ObservedElement, delta_y: int) -> bool:
        """Scroll an identified container without falling back to desktop coordinates."""
        if el.tag not in MAC_SCROLL_AREAS or not delta_y:
            return False
        return self._mac_gesture("macos: scroll", el, deltaX=0, deltaY=delta_y)

    def scroll_into_view(self, el: ObservedElement) -> None:
        """Android: bring a control below the fold into view before tapping it."""
        from appium.webdriver.common.appiumby import AppiumBy

        if self.platform != "android" or len(el.bbox) != 4:
            return
        if not hasattr(self, "_win_h"):
            self._win_h = self.driver.get_window_size()["height"]
        if el.bbox[1] + el.bbox[3] // 2 < self._win_h - 40:
            return
        sel = None
        if el.selector.startswith("~"):
            sel = f'new UiSelector().description("{el.selector[1:]}")'
        elif el.selector.startswith("id:"):
            sel = f'new UiSelector().resourceId("{el.selector[3:]}")'
        elif el.name:
            sel = f'new UiSelector().text("{el.name}")'
        if sel:
            try:
                self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f"new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView({sel})")
            except Exception:
                pass

    def tap(self, el: ObservedElement) -> bool:
        self.scroll_into_view(el)
        target = self.find(el)
        if target is not None:
            try:
                target.click()
                return True
            except Exception:
                pass
        if self.platform == "macos":
            return False  # stale desktop coordinates can target a different app or window
        if len(el.bbox) == 4:
            x, y, w, h = el.bbox
            try:
                if self.platform == "android":
                    self.driver.execute_script("mobile: clickGesture", {"x": x + w // 2, "y": y + h // 2})
                else:
                    self.driver.execute_script("macos: click", {"x": x + w // 2, "y": y + h // 2})
                return True
            except Exception:
                return False
        return False

    def type_into(self, el: ObservedElement, value: str) -> bool:
        target = self.find(el)
        if target is None and self.platform != "macos" and len(el.bbox) == 4:
            self.tap(el)
            target = self.driver.switch_to.active_element
        if target is None:
            return False
        try:
            target.clear()
            if value:
                target.send_keys(value)
            return True
        except Exception:
            return False

    def read_value(self, el: ObservedElement, *, strict: bool = False) -> str:
        target = self.find(el)
        if target is None:
            if strict:
                raise RuntimeError("The field could not be located")
            return ""
        try:
            if self.platform == "macos":
                value = target.get_attribute("value")
            else:
                value = target.text or target.get_attribute("text") or target.get_attribute("value")
        except Exception as exc:
            if strict:
                raise RuntimeError("The field value could not be read") from exc
            return ""
        if strict and not isinstance(value, str):
            raise RuntimeError("The field does not expose a readable text value")
        return value or ""

    def find_text(self, text: str, scroll: bool = True) -> bool:
        """Whether a text or label containing `text` is on screen; on Android a scrollable list is scrolled to look for it."""
        if not text:
            return False
        snap = self.snapshot(0.3)
        if any(text in t for t in snap.texts) or any(text in (e.name or "") for e in snap.elements):
            return True
        if self.platform != "android" or not scroll:
            return False
        from appium.webdriver.common.appiumby import AppiumBy

        q = text.replace("\\", "\\\\").replace('"', '\\"')
        for sel in (f'new UiSelector().textContains("{q}")', f'new UiSelector().descriptionContains("{q}")'):
            try:
                self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f"new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView({sel})")
                return True
            except Exception:
                pass
        return False

    def back(self) -> None:
        if self.platform == "android":
            self.driver.back()
        elif self.platform == "macos":
            self.keys(["XCUIKeyboardKeyEscape"])

    def undo(self) -> None:
        if self.platform == "macos":
            self.keys([{"key": "z", "modifierFlags": 1 << 4}])  # XCUIKeyModifierCommand

    def keys(self, keys: list) -> None:
        if self.platform == "macos":
            self.driver.execute_script("macos: keys", {"keys": [{"key": k} if isinstance(k, str) else k for k in keys]})

    def hide_keyboard(self) -> None:
        if self.platform == "android":
            try:
                if self.driver.is_keyboard_shown():
                    self.driver.hide_keyboard()
            except Exception:
                pass

    def activate(self) -> None:
        if not self.build.app_id:
            return
        try:
            if self.platform == "macos":
                self.driver.execute_script("macos: activateApp", self._mac_target())
            else:
                self.driver.activate_app(self.build.app_id)
        except Exception as exc:
            logger.warning("could not activate %s: %s", self.build.app_id, exc)

    def _mac_target(self) -> dict[str, str]:
        return {"path": self.build.launch_path} if self.build.launch_path else {"bundleId": self.build.app_id}

    def relaunch(self) -> None:
        if self.build.app_id:
            try:
                self.driver.terminate_app(self.build.app_id)
            except Exception:
                pass
            time.sleep(1)
            self.driver.activate_app(self.build.app_id)
            time.sleep(2)

    def background(self, seconds: int = 3) -> None:
        self.driver.background_app(seconds)

    def rotate(self, landscape: bool) -> bool:
        if self.platform != "android":
            return False
        try:
            self.driver.orientation = "LANDSCAPE" if landscape else "PORTRAIT"
            return True
        except Exception:
            return False

    def set_network(self, on: bool) -> bool:
        if self.platform != "android" or self.driver is None:
            return False
        try:
            self.driver.execute_script("mobile: setConnectivity", {"wifi": on, "data": on})
            return True
        except Exception:
            try:
                adb = _adb()
                for svc in ("wifi", "data"):
                    subprocess.run([adb, "-s", self.serial, "shell", "svc", svc, "enable" if on else "disable"], capture_output=True, timeout=20)
                return True
            except Exception:
                return False
