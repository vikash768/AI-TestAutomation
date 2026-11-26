"""Store Android uploads outside the public artifact directory."""
from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

MAX_APK_UPLOAD_BYTES = 512 * 1024 * 1024


class ApkUploadTooLarge(ValueError):
    pass


def apk_upload_path(artifacts_dir: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise ValueError("Invalid run ID for APK upload")
    directory = artifacts_dir.absolute()
    return directory.parent / f".{directory.name}-uploads" / f"{run_id}.apk"


def save_apk_upload(artifacts_dir: Path, run_id: str, stream: BinaryIO) -> Path:
    """Copy and validate the APK before the application queues its run."""
    path = apk_upload_path(artifacts_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = path.open("xb")
    try:
        bytes_written = 0
        with destination:
            while chunk := stream.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > MAX_APK_UPLOAD_BYTES:
                    limit_mib = MAX_APK_UPLOAD_BYTES // (1024 * 1024)
                    raise ApkUploadTooLarge(f"APK uploads are limited to {limit_mib} MiB")
                destination.write(chunk)
        try:
            with ZipFile(path) as archive:
                archive.getinfo("AndroidManifest.xml")
        except (BadZipFile, KeyError, UnicodeError) as exc:
            raise ValueError("Upload a valid APK containing AndroidManifest.xml") from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
