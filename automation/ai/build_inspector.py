"""Inspect native application build artifacts for AI test grounding."""

from __future__ import annotations

import hashlib
import plistlib
import re
import zipfile
from pathlib import Path
from typing import Any


class BuildInspector:
    """Extract safe, deterministic metadata from APK, IPA, app, and EXE builds."""

    def __init__(self, build_path: str):
        self._build_path = Path(build_path).expanduser()

    def inspect(self) -> dict[str, Any]:
        path = self._build_path
        result: dict[str, Any] = {
            "build_path": str(path),
            "exists": path.exists(),
            "artifact_type": self._artifact_type(path),
            "summary_prompt": "",
        }
        if not path.exists():
            result["error"] = f"Build artifact does not exist: {path}"
            return result

        try:
            if path.is_file():
                result["size_bytes"] = path.stat().st_size
                result["sha256"] = self._sha256(path)

            artifact_type = result["artifact_type"]
            if artifact_type == "apk":
                result.update(self._inspect_apk(path))
            elif artifact_type == "ipa":
                result.update(self._inspect_ipa(path))
            elif artifact_type == "macos_app":
                result.update(self._inspect_app(path))

            result["summary_prompt"] = self._summary(result)
        except (OSError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
            result["error"] = f"Unable to inspect build artifact: {exc}"
        return result

    @staticmethod
    def _artifact_type(path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".apk": "apk",
            ".ipa": "ipa",
            ".app": "macos_app",
            ".exe": "windows_exe",
        }.get(suffix, "unknown")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _inspect_apk(self, path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            abis = sorted({name.split("/")[1] for name in names if name.startswith("lib/") and len(name.split("/")) > 2})
            bundles = [name for name in names if name.endswith((".bundle", ".jsbundle"))][:10]
            ui_hints: set[str] = set()
            for bundle_name in bundles[:2]:
                info = archive.getinfo(bundle_name)
                if info.file_size > 30 * 1024 * 1024:
                    continue
                data = archive.read(bundle_name)
                text = data.decode("utf-8", errors="ignore")
                for match in re.findall(
                    r'(?:testID|accessibilityLabel|placeholder)["\']?\s*[:=]\s*["\']([^"\']{2,80})["\']',
                    text,
                ):
                    ui_hints.add(match.strip())
                    if len(ui_hints) >= 50:
                        break
            return {
                "archive_entries": len(names),
                "abis": abis,
                "javascript_bundles": bundles,
                "ui_hints": sorted(ui_hints),
            }

    def _inspect_ipa(self, path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            plist_names = [name for name in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name)]
            metadata: dict[str, Any] = {"archive_entries": len(names)}
            if plist_names:
                info = plistlib.loads(archive.read(plist_names[0]))
                metadata.update(self._plist_metadata(info))
            return metadata

    def _inspect_app(self, path: Path) -> dict[str, Any]:
        plist_path = path / "Contents" / "Info.plist"
        metadata: dict[str, Any] = {}
        if plist_path.is_file():
            with plist_path.open("rb") as handle:
                metadata.update(self._plist_metadata(plistlib.load(handle)))
        resources = path / "Contents" / "Resources"
        if resources.is_dir():
            metadata["resource_files"] = sum(1 for item in resources.rglob("*") if item.is_file())
        return metadata

    @staticmethod
    def _plist_metadata(info: dict[str, Any]) -> dict[str, Any]:
        return {
            "bundle_id": info.get("CFBundleIdentifier", ""),
            "bundle_name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
            "version": info.get("CFBundleShortVersionString", ""),
            "build_number": info.get("CFBundleVersion", ""),
            "executable": info.get("CFBundleExecutable", ""),
        }

    @staticmethod
    def _summary(result: dict[str, Any]) -> str:
        lines = [f"Build type: {result.get('artifact_type', 'unknown')}"]
        if result.get("size_bytes") is not None:
            lines.append(f"Build size: {result['size_bytes']} bytes")
        if result.get("sha256"):
            lines.append(f"Build SHA-256: {result['sha256']}")
        for key, label in (
            ("bundle_id", "Bundle ID"),
            ("bundle_name", "Bundle name"),
            ("version", "Version"),
            ("build_number", "Build number"),
            ("abis", "Native ABIs"),
            ("javascript_bundles", "Embedded JS bundles"),
            ("ui_hints", "Build UI hints"),
        ):
            value = result.get(key)
            if value:
                rendered = ", ".join(value[:30]) if isinstance(value, list) else str(value)
                lines.append(f"{label}: {rendered}")
        return "\n".join(f"- {line}" for line in lines)
