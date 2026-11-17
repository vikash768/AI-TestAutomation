"""Worker heartbeats: each worker rewrites artifacts/workers/<host>-<pid>.json every
few seconds (from a thread, so also mid-run). The API reads them to tell a queued
run apart from one nobody will ever pick up. artifacts/ is shared between the host
and the containers, so the host's app worker is visible to the API container.
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import threading
import time
from pathlib import Path

HEARTBEAT_SECONDS = 5.0


def heartbeat_dir(artifacts_dir: Path) -> Path:
    return Path(artifacts_dir) / "workers"


def _alive(beat: dict, max_age: float) -> bool:
    if time.time() - float(beat.get("ts", 0)) > max_age:
        return False
    if beat.get("host") == socket.gethostname() and beat.get("pid"):
        try:
            os.kill(int(beat["pid"]), 0)  # same machine: the process itself must still exist
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    return True


def live_platforms(artifacts_dir: Path, max_age: float = 30.0) -> set[str]:
    """Platforms some worker has claimed recently (each worker rewrites its heartbeat file every few seconds, even mid-run)."""
    out: set[str] = set()
    now = time.time()
    for f in heartbeat_dir(artifacts_dir).glob("*.json"):
        try:
            beat = json.loads(f.read_text())
        except Exception:
            continue
        if _alive(beat, max_age):
            out.update(beat.get("platforms", []))
        elif now - float(beat.get("ts", 0)) > 86400:
            f.unlink(missing_ok=True)  # a worker that was killed long ago
    return out


def live_runs(artifacts_dir: Path, max_age: float = 30.0) -> set[str]:
    """Runs a live worker says it is executing."""
    out: set[str] = set()
    for f in heartbeat_dir(artifacts_dir).glob("*.json"):
        try:
            beat = json.loads(f.read_text())
        except Exception:
            continue
        if beat.get("run") and _alive(beat, max_age):
            out.add(beat["run"])
    return out


def _beat(path: Path, platforms: tuple[str, ...], state: dict) -> None:
    while True:
        try:
            path.write_text(json.dumps({"platforms": list(platforms), "pid": os.getpid(), "host": socket.gethostname(), "ts": time.time(), "run": state.get("run")}))
        except Exception:
            pass
        time.sleep(HEARTBEAT_SECONDS)


def start_heartbeat(artifacts_dir: Path, platforms: tuple[str, ...]) -> dict:
    """Start beating; returns the dict whose "run" key the worker updates."""
    state: dict = {}
    beats = heartbeat_dir(artifacts_dir)
    beats.mkdir(parents=True, exist_ok=True)
    path = beats / f"{socket.gethostname()}-{os.getpid()}.json"
    threading.Thread(target=_beat, args=(path, platforms, state), daemon=True).start()
    atexit.register(lambda: path.unlink(missing_ok=True))
    return state
