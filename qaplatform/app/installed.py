"""Share the macOS worker's installed app choices with the API container."""
from __future__ import annotations

import atexit
import json
import logging
import os
import plistlib
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from xml.parsers.expat import ExpatError

from qaplatform.app.build import APP_ID_RE
from qaplatform.heartbeat import _alive, heartbeat_dir

REFRESH_SECONDS = 60
HEARTBEAT_MAX_AGE = 30
SNAPSHOT_MAX_AGE = REFRESH_SECONDS * 2
APP_FIELDS = ("name", "path", "bundle_id")
logger = logging.getLogger(__name__)


def _application_roots() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications", Path("/System/Applications"),
            Path("/System/Library/CoreServices/Applications"))


def _bundle_info(bundle: Path) -> dict[str, str] | None:
    try:
        plist = (bundle / "Contents/Info.plist").resolve(strict=True)
        if not plist.is_relative_to(bundle) or not plist.is_file():
            return None
        with plist.open("rb") as source:
            metadata = plistlib.load(source)
    except (OSError, ValueError, RuntimeError, ExpatError, plistlib.InvalidFileException):
        return None
    if not isinstance(metadata, dict):
        return None
    bundle_id = metadata.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not APP_ID_RE.fullmatch(bundle_id):
        return None
    name = next((label.strip() for key in ("CFBundleDisplayName", "CFBundleName")
                 if isinstance(label := metadata.get(key), str) and label.strip()), bundle.stem)
    return {"name": name, "path": str(bundle), "bundle_id": bundle_id}


def discover_macos_apps(roots: Iterable[Path] | None = None) -> list[dict[str, str]]:
    """List distinct app paths, including multiple builds that share a bundle identifier."""
    apps: dict[str, dict[str, str]] = {}
    for root in roots if roots is not None else _application_roots():
        if root.is_symlink():
            continue
        for parent, directories, _ in os.walk(root.resolve(), followlinks=False):
            children = []
            for name in sorted(directories, key=lambda value: (value.casefold(), value)):
                path = Path(parent) / name
                if name.startswith(".") or path.is_symlink():
                    continue
                if path.suffix.lower() == ".app":
                    if app := _bundle_info(path):
                        apps.setdefault(app["path"], app)
                else:
                    children.append(name)
            directories[:] = children
    return sorted(apps.values(), key=lambda app: (app["name"].casefold(), app["bundle_id"].casefold(), app["path"]))


def _inventory_dir(artifacts_dir: Path) -> Path:
    return heartbeat_dir(artifacts_dir) / "macos-apps"


def _write_snapshot(path: Path, apps: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump({"ts": time.time(), "apps": apps}, file)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def start_macos_app_inventory(artifacts_dir: Path) -> None:
    """Publish a fresh catalog even while the native worker is running a test."""
    if sys.platform != "darwin":
        return
    path = _inventory_dir(artifacts_dir) / f"{socket.gethostname()}-{os.getpid()}.json"
    stopped = threading.Event()

    def refresh() -> None:
        while not stopped.is_set():
            try:
                apps = discover_macos_apps()
                if not stopped.is_set():
                    _write_snapshot(path, apps)
            except OSError:
                logger.warning("Could not publish installed macOS apps", exc_info=True)
            stopped.wait(REFRESH_SECONDS)

    def cleanup() -> None:
        stopped.set()
        path.unlink(missing_ok=True)

    threading.Thread(target=refresh, name="macos-app-inventory", daemon=True).start()
    atexit.register(cleanup)


def read_macos_app_inventory(artifacts_dir: Path) -> dict:
    """Return the most recent catalog whose publishing macOS worker is still live."""
    records = []
    for snapshot in _inventory_dir(artifacts_dir).glob("*.json"):
        if snapshot.is_symlink():
            continue
        try:
            heartbeat = json.loads((heartbeat_dir(artifacts_dir) / snapshot.name).read_text(encoding="utf-8"))
            if "macos" not in heartbeat.get("platforms", []) or not _alive(heartbeat, HEARTBEAT_MAX_AGE):
                continue
            record = json.loads(snapshot.read_text(encoding="utf-8"))
            timestamp = float(record["ts"])
            if not 0 <= time.time() - timestamp <= SNAPSHOT_MAX_AGE or not isinstance(record["apps"], list):
                continue
            apps = [{key: app[key] for key in APP_FIELDS} for app in record["apps"]
                    if isinstance(app, dict) and all(isinstance(app.get(key), str) and app[key] for key in APP_FIELDS)]
            records.append((timestamp, snapshot.name, apps))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    if not records:
        return {"available": False, "apps": []}
    return {"available": True, "apps": max(records)[2]}
