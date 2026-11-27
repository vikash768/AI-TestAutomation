import json
import plistlib
import time

from qaplatform.app.installed import _write_snapshot, discover_macos_apps, read_macos_app_inventory


def _bundle(path, bundle_id, *, name=None, binary=False):
    info = path / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    metadata = {"CFBundleIdentifier": bundle_id}
    if name is not None:
        metadata["CFBundleDisplayName"] = name
    info.write_bytes(plistlib.dumps(metadata, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML))
    return path


def test_discovery_lists_distinct_app_paths_with_shared_bundle_ids(tmp_path):
    applications = tmp_path / "Applications"
    user_apps = tmp_path / "User Applications"
    editor = _bundle(applications / "Utilities/Editor.app", "org.example.Editor", name="Editor", binary=True)
    checklist = _bundle(user_apps / "Checklist.app", "org.example.Checklist", name="checklist")
    fallback = _bundle(user_apps / "Notes.app", "org.example.Notes")

    editor_copy = _bundle(user_apps / "Editor Copy.app", "org.example.Editor", name="Editor")
    _bundle(editor / "Contents/Helpers/Hidden.app", "org.example.Hidden")
    _bundle(applications / ".Archive/Archived.app", "org.example.Archived")
    _bundle(applications / "No ID.app", "Invalid id")
    malformed_info = applications / "Unreadable.app/Contents/Info.plist"
    malformed_info.parent.mkdir(parents=True)
    malformed_info.write_text('<?xml version="1.0"?><plist><dict><')

    outsider = _bundle(tmp_path / "Outside/External.app", "org.example.External")
    (applications / "Alias.app").symlink_to(outsider, target_is_directory=True)
    (applications / "Linked folder").symlink_to(outsider.parent, target_is_directory=True)
    linked_info = applications / "Linked Info.app/Contents/Info.plist"
    linked_info.parent.mkdir(parents=True)
    linked_info.symlink_to(outsider / "Contents/Info.plist")

    assert discover_macos_apps((applications, user_apps, applications / "Utilities")) == [
        {"name": "checklist", "bundle_id": "org.example.Checklist", "path": str(checklist.resolve())},
        {"name": "Editor", "bundle_id": "org.example.Editor", "path": str(editor.resolve())},
        {"name": "Editor", "bundle_id": "org.example.Editor", "path": str(editor_copy.resolve())},
        {"name": "Notes", "bundle_id": "org.example.Notes", "path": str(fallback.resolve())},
    ]


def test_reads_latest_snapshot_from_live_macos_worker(tmp_path):
    workers = tmp_path / "workers"
    snapshots = workers / "macos-apps"
    valid = {"name": "Editor", "bundle_id": "org.example.Editor", "path": "/Applications/Editor.app"}
    hidden = {"name": "Private", "bundle_id": "org.example.Private", "path": "/Applications/Private.app"}

    def publish(host, platforms, age, apps):
        _write_snapshot(snapshots / f"{host}-1.json", apps)
        (workers / f"{host}-1.json").write_text(json.dumps({"host": host, "pid": 1, "platforms": platforms, "ts": time.time() - age}))

    assert read_macos_app_inventory(tmp_path) == {"available": False, "apps": []}
    publish("older-worker", ["macos"], 1, [hidden])
    publish("picker-worker", ["macos", "android"], 1, [valid | {"unrelated": "private fixture"}])
    publish("web-worker", ["web"], 1, [hidden])
    publish("stopped-worker", ["macos"], 60, [hidden])
    _write_snapshot(snapshots / "missing-heartbeat-1.json", [hidden])
    assert set(path.suffix for path in snapshots.iterdir()) == {".json"}
    assert read_macos_app_inventory(tmp_path) == {"available": True, "apps": [valid]}

    (workers / "picker-worker-1.json").unlink()
    (snapshots / "older-worker-1.json").write_text(json.dumps({"ts": time.time() - 180, "apps": [hidden]}))
    assert read_macos_app_inventory(tmp_path) == {"available": False, "apps": []}

    publish("empty-worker", ["macos"], 1, [])
    assert read_macos_app_inventory(tmp_path) == {"available": True, "apps": []}
