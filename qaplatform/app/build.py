"""Resolve and inspect the build under test: .apk, .ipa, .app, .dmg, or an installed app id.

The result says how Appium should launch the app (install a file, or start an
already-installed package / bundle) and carries metadata for the report.
Nothing here modifies the build.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

KIND_OF_SUFFIX = {".apk": "apk", ".aab": "aab", ".ipa": "ipa", ".app": "app", ".dmg": "dmg"}
PLATFORM_OF_KIND = {"apk": "android", "ipa": "ios", "app": "macos", "dmg": "macos"}
APP_ID_RE = re.compile(r"^[A-Za-z][\w-]*(\.[A-Za-z0-9_-]+)+$")
logger = logging.getLogger(__name__)


@dataclass
class BuildInfo:
    platform: str
    kind: str  # apk | ipa | app | dmg | installed
    path: str = ""
    launch_path: str = ""  # .app inside a mounted DMG, or the file itself
    app_id: str = ""  # Android package or Apple bundle id
    activity: str = ""
    label: str = ""
    version: str = ""
    sha256: str = ""
    size_bytes: int = 0
    framework: str = ""  # react-native | flutter | electron | native
    extra: dict = field(default_factory=dict)
    _dmg_device: str = field(default="", repr=False)

    @property
    def owns_mount(self) -> bool:
        return bool(self._dmg_device)

    def close(self) -> None:
        """Detach a disk image attached during this run after the Appium session ends."""
        if not self._dmg_device:
            return
        try:
            subprocess.run(["hdiutil", "detach", self._dmg_device], capture_output=True, timeout=20, check=True)
        except (OSError, subprocess.SubprocessError):
            logger.warning("Could not detach the macOS build image %s", self._dmg_device, exc_info=True)
        else:
            self._dmg_device = ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aapt2() -> str | None:
    found = shutil.which("aapt2")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or str(Path.home() / "Library/Android/sdk")
    candidates = sorted(glob.glob(f"{sdk}/build-tools/*/aapt2"))
    return candidates[-1] if candidates else None


def _inspect_apk(path: Path, info: BuildInfo) -> None:
    tool = _aapt2()
    if tool:
        proc = subprocess.run([tool, "dump", "badging", str(path)], capture_output=True, text=True, timeout=60)
        out = proc.stdout
        if m := re.search(r"package: name='([^']+)'.*?versionName='([^']*)'", out):
            info.app_id, info.version = m.group(1), m.group(2)
        if m := re.search(r"launchable-activity: name='([^']+)'", out):
            info.activity = m.group(1)
        if m := re.search(r"application-label:'([^']*)'", out):
            info.label = m.group(1)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    if any(n.endswith(("libhermes.so", "libhermesvm.so", "libreactnative.so", "libreactnativejni.so")) or n.endswith("index.android.bundle") for n in names):
        info.framework = "react-native"
    elif any(n.endswith("libflutter.so") for n in names):
        info.framework = "flutter"
    else:
        info.framework = "native"
    info.extra["abis"] = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") > 1})


def _plist(info: BuildInfo, plist: dict) -> None:
    info.app_id = plist.get("CFBundleIdentifier", "")
    info.label = plist.get("CFBundleDisplayName") or plist.get("CFBundleName", "")
    info.version = plist.get("CFBundleShortVersionString", "")


def _inspect_app_bundle(app: Path, info: BuildInfo) -> None:
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise RuntimeError(f"Not a macOS app bundle (Contents/Info.plist missing): {app}")
    with plist_path.open("rb") as fh:
        _plist(info, plistlib.load(fh))
    if not info.app_id:
        raise RuntimeError(f"macOS app bundle has no CFBundleIdentifier: {app}")
    if (app / "Contents" / "Frameworks" / "Electron Framework.framework").exists():
        info.framework = "electron"
    else:
        info.framework = "native"


def _mount_dmg(path: Path, info: BuildInfo) -> Path:
    """Attach a DMG read-only (or reuse its mount) and return the single .app inside."""
    if sys.platform != "darwin":
        raise RuntimeError("DMG builds can only be mounted on macOS")
    listing = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, timeout=20, check=True)
    image = next((image for image in plistlib.loads(listing.stdout).get("images", [])
                  if image.get("image-path") and Path(image["image-path"]).resolve() == path.resolve()), None)
    if image is None:
        attached = subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(path)], capture_output=True, timeout=120, check=True)
        image = plistlib.loads(attached.stdout)
        entities = image.get("system-entities", [])
        info._dmg_device = next((e["dev-entry"] for e in entities if re.fullmatch(r"/dev/disk\d+", e.get("dev-entry", ""))), "")
        if not info.owns_mount:
            info._dmg_device = next((e["mount-point"] for e in entities if e.get("mount-point")), "")
    volumes = [Path(e["mount-point"]) for e in image.get("system-entities", []) if e.get("mount-point")]
    apps = sorted(a for v in volumes for a in v.glob("*.app") if a.is_dir() and not a.is_symlink())
    if len(apps) != 1:
        raise RuntimeError(f"Expected exactly one .app in {path.name}; found {len(apps)}")
    return apps[0]


def resolve_build(spec: str, platform: str = "") -> BuildInfo:
    """`spec` is a build file path or an installed app id (com.example.app)."""
    spec = (spec or "").strip()
    path = Path(spec).expanduser()
    if spec and not path.exists() and APP_ID_RE.match(spec):
        if not platform or platform == "web":
            raise ValueError(f"'{spec}' looks like an installed app id; choose the platform it is installed on")
        return BuildInfo(platform=platform, kind="installed", app_id=spec)
    if not path.exists():
        hint = ""
        if str(path).startswith(str(Path.home() / "Downloads")) or str(path).startswith(str(Path.home() / "Desktop")):
            hint = (" — if the file is there, macOS privacy settings may be blocking this process: grant it Files and Folders / Full Disk Access, "
                    "or copy the build into artifacts/builds/")
        raise FileNotFoundError(f"Build not found or not readable: {path}{hint}")
    kind = KIND_OF_SUFFIX.get(path.suffix.lower())
    if kind is None:
        raise ValueError(f"Unsupported build type '{path.suffix}'. Use .apk (Android), .ipa (iOS), .app or .dmg (macOS)")
    if kind == "aab":
        raise ValueError("An .aab is a Play Store bundle and cannot be installed directly; build an .apk (e.g. ./gradlew assembleRelease)")
    detected = PLATFORM_OF_KIND[kind]
    if platform and platform != detected:
        raise ValueError(f"A .{kind} build runs on {detected}, but the run asks for {platform}")
    info = BuildInfo(platform=detected, kind=kind, path=str(path.resolve()), launch_path=str(path.resolve()))
    try:
        if path.is_file():
            info.size_bytes = path.stat().st_size
            info.sha256 = _sha256(path)
        if kind == "apk":
            _inspect_apk(path, info)
        elif kind == "ipa":
            with zipfile.ZipFile(path) as zf:
                plists = [n for n in zf.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)]
                if plists:
                    _plist(info, plistlib.loads(zf.read(plists[0])))
        elif kind == "dmg":
            app = _mount_dmg(path, info)
            info.launch_path = str(app)
            _inspect_app_bundle(app, info)
        elif kind == "app":
            _inspect_app_bundle(path, info)
    except PermissionError as exc:
        info.close()
        raise PermissionError(f"Cannot read {path}: {exc}. Grant this process access to the folder, or copy the build into artifacts/builds/") from exc
    except BaseException:
        info.close()
        raise
    return info
