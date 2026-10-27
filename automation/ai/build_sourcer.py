"""
Build Sourcer — Resolve or fetch application build artifacts.

Given a cloned repository and target platform, this module locates existing
build artifacts (.apk, .ipa, .dmg, .app, .exe) or resolves live web URLs
for testing. It integrates with the unified pipeline so that build paths are
seamlessly passed to Appium/Selenium configurations.

Strategies per platform:
    Android  → Scan for .apk in standard Gradle/RN output dirs, or pull from CI
    iOS      → Scan for .ipa or .app in Xcode derived data
    macOS    → Scan for .dmg or .app in dist/, build/
    Windows  → Scan for .exe in dist/, build/, Release/
    Web      → Resolve live/staging URL from package.json, env vars, or detect dev server
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Standard locations where build artifacts are commonly found per platform.
_ANDROID_SEARCH_PATHS: list[str] = [
    "app/build/outputs/apk/debug",
    "app/build/outputs/apk/release",
    "android/app/build/outputs/apk/debug",
    "android/app/build/outputs/apk/release",
    "build/outputs/apk",
    "artifacts/builds",
    "dist",
    "build",
]

_IOS_SEARCH_PATHS: list[str] = [
    "ios/build/Build/Products",
    "build/ios",
    "dist",
    "artifacts/builds",
]

_MACOS_SEARCH_PATHS: list[str] = [
    "dist",
    "build",
    "out",
    "release",
    "artifacts/builds",
]

_WINDOWS_SEARCH_PATHS: list[str] = [
    "dist",
    "build",
    "out",
    "Release",
    "release",
    "artifacts/builds",
]


class BuildSourcer:
    """Resolve or fetch application build artifacts for test execution.

    This module is invoked by the unified pipeline after repository analysis.
    It ensures that the correct build artifact path is available before
    Appium/Selenium sessions are created.
    """

    def __init__(
        self,
        repo_path: str | Path | None = None,
        platform: str = "web",
        artifacts_dir: str | Path = "artifacts",
    ) -> None:
        self._repo_path = Path(repo_path) if repo_path else None
        self._platform = platform.lower()
        self._artifacts_dir = Path(artifacts_dir)
        self._builds_dir = self._artifacts_dir / "builds"

    def resolve(
        self,
        *,
        app_build_path: str = "",
        base_url: str = "",
        bundle_id: str = "",
        app_package: str = "",
    ) -> dict[str, Any]:
        """Resolve the build artifact or web URL for the target platform.

        Args:
            app_build_path: Explicit path to build artifact (if already known).
            base_url: Explicit web URL (for web targets).
            bundle_id: iOS/macOS bundle ID (fallback identification).
            app_package: Android package name (fallback identification).

        Returns:
            A dict with keys:
                resolved_path: str — path to the resolved build artifact
                resolved_url: str — resolved web URL (web targets only)
                source: str — how the artifact was found
                error: str — empty if successful
        """
        result: dict[str, Any] = {
            "resolved_path": "",
            "resolved_url": "",
            "source": "",
            "error": "",
            "platform": self._platform,
        }

        if self._platform == "web":
            return self._resolve_web(result, base_url=base_url)
        elif self._platform == "android":
            return self._resolve_native(
                result,
                explicit_path=app_build_path,
                extensions={".apk"},
                search_paths=_ANDROID_SEARCH_PATHS,
                identifier=app_package,
            )
        elif self._platform == "ios":
            return self._resolve_native(
                result,
                explicit_path=app_build_path,
                extensions={".ipa", ".app"},
                search_paths=_IOS_SEARCH_PATHS,
                identifier=bundle_id,
            )
        elif self._platform == "macos":
            return self._resolve_native(
                result,
                explicit_path=app_build_path,
                extensions={".dmg", ".app"},
                search_paths=_MACOS_SEARCH_PATHS,
                identifier=bundle_id,
            )
        elif self._platform == "windows":
            return self._resolve_native(
                result,
                explicit_path=app_build_path,
                extensions={".exe", ".msi"},
                search_paths=_WINDOWS_SEARCH_PATHS,
                identifier="",
            )
        else:
            result["error"] = f"Unsupported platform: {self._platform}"
            return result

    def _resolve_web(self, result: dict[str, Any], base_url: str = "") -> dict[str, Any]:
        """Resolve web application URL."""
        # 1. Explicit URL
        if base_url:
            result["resolved_url"] = base_url
            result["source"] = "explicit"
            logger.info("Web URL resolved (explicit): %s", base_url)
            return result

        # 2. Environment variable
        env_url = os.environ.get("BASE_URL", "")
        if env_url:
            result["resolved_url"] = env_url
            result["source"] = "environment"
            logger.info("Web URL resolved (env): %s", env_url)
            return result

        # 3. Try to extract from package.json homepage
        if self._repo_path:
            pkg_path = self._repo_path / "package.json"
            if pkg_path.is_file():
                try:
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                    homepage = pkg.get("homepage", "")
                    if homepage and homepage.startswith("http"):
                        result["resolved_url"] = homepage
                        result["source"] = "package.json/homepage"
                        logger.info("Web URL resolved (package.json): %s", homepage)
                        return result
                except (OSError, json.JSONDecodeError):
                    pass

        result["error"] = (
            "No web URL found. Set BASE_URL in .env or pass --base-url."
        )
        return result

    def _resolve_native(
        self,
        result: dict[str, Any],
        explicit_path: str,
        extensions: set[str],
        search_paths: list[str],
        identifier: str,
    ) -> dict[str, Any]:
        """Resolve a native build artifact (.apk, .ipa, .dmg, .exe, etc.)."""
        # 1. Explicit path provided
        if explicit_path:
            path = Path(explicit_path).expanduser()
            if path.exists():
                result["resolved_path"] = str(path)
                result["source"] = "explicit"
                logger.info(
                    "Build artifact resolved (explicit): %s", path,
                )
                return result
            else:
                result["error"] = f"Explicit build path does not exist: {path}"
                return result

        # 2. Scan the repository for build artifacts
        if self._repo_path and self._repo_path.is_dir():
            found = self._scan_for_artifacts(
                self._repo_path, extensions, search_paths
            )
            if found:
                result["resolved_path"] = str(found)
                result["source"] = "repo_scan"
                logger.info(
                    "Build artifact resolved (repo scan): %s", found,
                )
                return result

        # 3. Scan the builds directory for pre-placed artifacts
        if self._builds_dir.is_dir():
            found = self._scan_for_artifacts(
                self._builds_dir, extensions, [""]
            )
            if found:
                result["resolved_path"] = str(found)
                result["source"] = "builds_dir"
                logger.info(
                    "Build artifact resolved (builds dir): %s", found,
                )
                return result

        # 4. If we have a bundle_id or package, report it as a fallback
        if identifier:
            result["source"] = "identifier_only"
            logger.info(
                "No build artifact found; using identifier: %s", identifier,
            )
            return result

        result["error"] = (
            f"No {', '.join(extensions)} artifact found for {self._platform}. "
            f"Set APP_BUILD_PATH in .env or place the artifact in artifacts/builds/."
        )
        return result

    def _scan_for_artifacts(
        self,
        base_dir: Path,
        extensions: set[str],
        search_dirs: list[str],
    ) -> Path | None:
        """Scan standard directories for matching build artifacts.

        Returns the most recently modified artifact found.
        """
        candidates: list[Path] = []

        for search_dir in search_dirs:
            scan_path = base_dir / search_dir if search_dir else base_dir
            if not scan_path.is_dir():
                continue
            for ext in extensions:
                # Only scan 2 levels deep to avoid traversing node_modules etc.
                for depth_pattern in [f"*{ext}", f"*/*{ext}"]:
                    candidates.extend(scan_path.glob(depth_pattern))

        if not candidates:
            return None

        # Return the most recently modified artifact
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    @staticmethod
    def platform_to_target_type(platform: str) -> str:
        """Convert a unified platform name to the internal target_type."""
        mapping = {
            "web": "web",
            "android": "mobile",
            "ios": "mobile",
            "macos": "desktop",
            "windows": "desktop",
        }
        return mapping.get(platform.lower(), "web")

    @staticmethod
    def platform_to_appium_platform(platform: str) -> str:
        """Convert a unified platform name to an Appium platform string."""
        if platform.lower() in ("android", "ios", "macos", "windows"):
            return platform.lower()
        return ""
