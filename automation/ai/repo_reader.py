"""
Repository Codebase Reader and Inspector.

Reads codebase from a repository URL (or local path) specified in .env (APP_REPO_URL),
extracts routes, component/screen names, testIDs, accessibility labels, and form fields,
and provides grounded context to the AI test generator.
"""

from __future__ import annotations

import logging
import json
import hashlib
import os
import re
import subprocess
import base64
from pathlib import Path
from typing import Any

from automation.secrets.sanitizer import sanitize_text

logger = logging.getLogger(__name__)

# File extensions to scan for frontend / app elements
SCANNABLE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".vue", ".svelte",
    ".dart", ".kt", ".java", ".swift", ".xml",
    ".py", ".rb", ".go", ".rs", ".cs",
}

# Patterns for detecting API endpoints across common backend frameworks
_API_PATTERNS: list[tuple[str, str]] = [
    # Express.js / Node
    (r'(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']', "express"),
    # FastAPI / Flask / Django
    (r'@(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']', "python"),
    # Spring Boot
    (r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']', "spring"),
    # Generic fetch/axios calls (client-side API consumption)
    (r'(?:fetch|axios\.(?:get|post|put|delete))\s*\(\s*[`"\']([/][^"\'`]+)[`"\']', "client"),
]

# Framework detection signals
_FRAMEWORK_SIGNALS: dict[str, list[str]] = {
    "react": ["react-dom", "\"react\":", "from 'react'", 'from "react"'],
    "react-native": ["react-native", "expo", "AppRegistry"],
    "next": ["next/", "\"next\":", "getServerSideProps", "getStaticProps"],
    "vue": ["\"vue\":", "createApp", ".vue"],
    "angular": ["@angular/core", "NgModule"],
    "flutter": ["package:flutter", "MaterialApp", "pubspec.yaml"],
    "electron": ["\"electron\":", "BrowserWindow", "electron.js"],
    "django": ["django", "urlpatterns", "views.py"],
    "fastapi": ["fastapi", "FastAPI()"],
    "flask": ["from flask", "Flask(__name__)"],
    "spring": ["SpringBootApplication", "@RestController"],
    "swiftui": ["import SwiftUI", "@main", "WindowGroup"],
    "kotlin-android": ["androidx", "AppCompatActivity", "setContentView"],
}

# Directories to always skip during scanning
IGNORED_DIRS = {
    "node_modules", ".git", ".github", "dist", "build", "coverage",
    ".next", ".nuxt", "Pods", ".gradle", "bin", "obj", ".idea", ".vscode",
    "venv", ".venv", "__pycache__", ".cache", "vendor",
}


class RepoReader:
    """Reads and inspects code from a git repository or local codebase."""

    def __init__(
        self,
        repo_url: str = "",
        cache_dir: str | Path | None = None,
        repo_local_path: str = "",
    ):
        self._repo_url = (repo_url or os.environ.get("APP_REPO_URL", "")).strip()
        self._cache_dir = Path(cache_dir or "artifacts/repos")
        self._repo_local_path = (
            repo_local_path or os.environ.get("APP_REPO_LOCAL_PATH", "")
        ).strip()
        self._repo_ref = os.environ.get("APP_REPO_REF", "").strip()
        self.last_error = ""

    @property
    def repo_url(self) -> str:
        return self._repo_url

    def fetch_or_locate_repo(self) -> Path | None:
        """Locate existing local repository or clone from git URL.

        Returns:
            Path to the local repository directory, or None if unavailable.
        """
        if not self._repo_url:
            return None

        # Inspect an existing checkout when its origin matches the configured
        # URL. This is especially useful for large repositories with submodules.
        if self._repo_local_path:
            checkout = Path(self._repo_local_path).expanduser().resolve()
            if not checkout.is_dir():
                self.last_error = f"Local repository does not exist: {checkout}"
                return None
            if not Path(self._repo_url).expanduser().is_dir():
                try:
                    origin = subprocess.run(
                        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    self.last_error = self._sanitize_error(str(exc))
                    return None
                if origin.returncode != 0 or self._canonical_url(origin.stdout) != self._canonical_url(self._repo_url):
                    self.last_error = f"Local repository origin does not match APP_REPO_URL: {checkout}"
                    return None
            logger.info("Using matching local checkout for %s: %s", self._repo_url, checkout)
            return checkout

        # Check if repo_url is an existing local directory
        local_path = Path(self._repo_url).expanduser()
        if local_path.is_dir():
            logger.info("Using local codebase directory: %s", local_path)
            return local_path

        # Otherwise treat as a git URL and clone to cache_dir
        repo_name = self._extract_repo_name(self._repo_url)
        repo_identity = self._canonical_url(self._repo_url) + "#" + self._repo_ref
        repo_hash = hashlib.sha256(repo_identity.encode("utf-8")).hexdigest()[:10]
        dest_dir = self._cache_dir / f"{repo_name}-{repo_hash}"

        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            if (dest_dir / ".git").is_dir():
                logger.info("Updating existing repository at %s...", dest_dir)
                try:
                    result = subprocess.run(
                        ["git", "-C", str(dest_dir), "pull", "--ff-only"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                        env=self._git_env(),
                    )
                    if result.returncode != 0:
                        self.last_error = self._sanitize_error(result.stderr or result.stdout)
                        logger.warning("Could not update cached repository: %s", self.last_error)
                        return None
                    submodules = subprocess.run(
                        ["git", "-C", str(dest_dir), "submodule", "update", "--init", "--recursive", "--depth", "1"],
                        capture_output=True,
                        text=True,
                        timeout=180,
                        check=False,
                        env=self._git_env(),
                    )
                    if submodules.returncode != 0:
                        self.last_error = self._sanitize_error(submodules.stderr or submodules.stdout)
                        logger.warning("Could not update repository submodules: %s", self.last_error)
                        return None
                except Exception as e:
                    self.last_error = self._sanitize_error(str(e))
                    logger.warning("Could not update repository %s: %s", dest_dir, self.last_error)
                    return None
                return dest_dir

            logger.info(
                "Cloning repository from %s into %s (depth=1)...",
                sanitize_text(self._repo_url),
                dest_dir,
            )
            clone_command = ["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules"]
            if self._repo_ref:
                clone_command.extend(["--branch", self._repo_ref])
            clone_command.extend([self._repo_url, str(dest_dir)])
            result = subprocess.run(
                clone_command,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
                env=self._git_env(),
            )
            if result.returncode == 0 and dest_dir.is_dir():
                logger.info("Repository cloned successfully to %s", dest_dir)
                return dest_dir
            else:
                err = self._sanitize_error(result.stderr or result.stdout or "Unknown git error")
                self.last_error = err
                logger.warning(
                    "Could not clone repository '%s' (exit code %d): %s",
                    sanitize_text(self._repo_url), result.returncode, err
                )
                return None
        except subprocess.TimeoutExpired:
            self.last_error = "Timed out while cloning the repository"
            logger.warning(
                "Timed out cloning repository '%s'. Proceeding with suite instructions.",
                sanitize_text(self._repo_url),
            )
            return None
        except Exception as e:
            self.last_error = self._sanitize_error(str(e))
            logger.warning(
                "Error fetching repository '%s': %s. Proceeding with suite instructions.",
                sanitize_text(self._repo_url),
                self.last_error,
            )
            return None

    def inspect_codebase(self, repo_path: Path | None = None, target_type: str = "web") -> dict[str, Any]:
        """Inspect the repository and extract routes, screens, testIDs, and form fields.

        Args:
            repo_path: Local path to inspected repository (fetches if None).
            target_type: Target platform ('web', 'mobile', 'desktop').

        Returns:
            A structured dict of discovered codebase elements and summary prompt.
        """
        path = repo_path or self.fetch_or_locate_repo()
        if not path or not path.is_dir():
            return {
                "repo_url": sanitize_text(self._repo_url),
                "repo_path": None,
                "repo_error": self.last_error,
                "scanned_files": 0,
                "routes": [],
                "screens": [],
                "test_ids": [],
                "accessibility_labels": [],
                "placeholders": [],
                "button_labels": [],
                "summary_prompt": "",
            }

        routes: set[str] = set()
        screens: set[str] = set()
        test_ids: set[str] = set()
        accessibility_labels: set[str] = set()
        placeholders: set[str] = set()
        button_labels: set[str] = set()
        ui_actions: set[str] = set()
        api_endpoints: set[str] = set()
        detected_frameworks: set[str] = set()
        form_fields: set[str] = set()

        # Regular expressions for common UI elements
        re_test_id = re.compile(r'(?:testID|data-testid)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_acc_label = re.compile(r'(?:accessibilityLabel|aria-label|contentDescription)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_placeholder = re.compile(r'placeholder\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_route_path = re.compile(r'(?:path|route)\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)
        re_screen_name = re.compile(r'<Screen\s+[^>]*name\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_button_title = re.compile(r'(?:title|text)\s*=\s*["\']([A-Z][a-zA-Z0-9\s]{2,25})["\']')
        re_ui_action = re.compile(r'(?:actions\.put|addAction)\s*\(\s*["\']([^"\']{2,60})["\']')
        re_form_field = re.compile(r'<(?:input|textarea|select)[^>]*(?:name|id)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

        # Compiled API endpoint patterns
        compiled_api_patterns = [
            (re.compile(pattern, re.IGNORECASE), source)
            for pattern, source in _API_PATTERNS
        ]

        scanned_count = 0
        full_content_sample = ""  # First 200KB for framework detection

        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for file in files:
                p = Path(root) / file
                if p.suffix.lower() not in SCANNABLE_EXTENSIONS:
                    continue
                if p.stat().st_size > 1024 * 1024:  # skip files > 1MB
                    continue

                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    scanned_count += 1

                    # Accumulate content for framework detection (limited)
                    if len(full_content_sample) < 200_000:
                        full_content_sample += content[:5000]

                    for match in re_test_id.findall(content):
                        test_ids.add(match.strip())
                    for match in re_acc_label.findall(content):
                        accessibility_labels.add(match.strip())
                    for match in re_placeholder.findall(content):
                        placeholders.add(match.strip())
                    for match in re_route_path.findall(content):
                        routes.add(match.strip())
                    for match in re_screen_name.findall(content):
                        screens.add(match.strip())
                    for match in re_button_title.findall(content):
                        button_labels.add(match.strip())
                    for match in re_form_field.findall(content):
                        form_fields.add(match.strip())
                    if target_type == "desktop" and p.name in {"Menus.js", "EditorUi.js"}:
                        ui_actions.update(re_ui_action.findall(content))

                    # Detect API endpoints
                    for pattern, source in compiled_api_patterns:
                        for api_match in pattern.finditer(content):
                            groups = api_match.groups()
                            # Extract the path (last captured group is typically the route)
                            endpoint = groups[-1].strip()
                            if endpoint and not endpoint.startswith("$"):
                                api_endpoints.add(endpoint)

                except Exception:
                    continue

        # Framework detection from accumulated content
        for framework, signals in _FRAMEWORK_SIGNALS.items():
            if any(signal in full_content_sample for signal in signals):
                detected_frameworks.add(framework)

        # Also check package.json for framework dependencies
        pkg_path = path / "package.json"
        if pkg_path.is_file():
            try:
                pkg_text = pkg_path.read_text(encoding="utf-8")
                for framework, signals in _FRAMEWORK_SIGNALS.items():
                    if any(signal in pkg_text for signal in signals):
                        detected_frameworks.add(framework)
            except OSError:
                pass

        # Check pubspec.yaml for Flutter
        pubspec = path / "pubspec.yaml"
        if pubspec.is_file():
            detected_frameworks.add("flutter")

        logger.info(
            "Scanned %d files in %s: %d testIDs, %d acc labels, %d placeholders, "
            "%d screens, %d routes, %d API endpoints, frameworks=%s",
            scanned_count, path, len(test_ids), len(accessibility_labels),
            len(placeholders), len(screens), len(routes), len(api_endpoints),
            sorted(detected_frameworks) or "(none)",
        )

        # Sort and limit for prompt compactness
        sorted_routes = sorted(routes)[:40]
        sorted_screens = sorted(screens)[:40]
        sorted_test_ids = sorted(test_ids)[:50]
        sorted_acc_labels = sorted(accessibility_labels)[:50]
        sorted_placeholders = sorted(placeholders)[:40]
        sorted_button_labels = sorted(button_labels)[:40]
        sorted_api_endpoints = sorted(api_endpoints)[:40]
        sorted_form_fields = sorted(form_fields)[:30]

        manifest: dict[str, str] = {}
        manifest_path = path / "package.json"
        if manifest_path.is_file():
            try:
                package = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = {
                    key: str(package[key])
                    for key in ("name", "version", "description", "main")
                    if isinstance(package.get(key), str)
                }
            except (OSError, ValueError):
                pass
        entrypoints = list(dict.fromkeys(
            relative for relative in (
                manifest.get("main", ""),
                "src/main/electron.js",
                "drawio/src/main/webapp/index.html",
            )
            if relative and (path / relative).is_file()
        ))
        resource_labels: dict[str, str] = {}
        resources = path / "drawio/src/main/webapp/resources/dia.txt"
        if target_type == "desktop" and resources.is_file():
            try:
                for line in resources.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        if key in {action.removesuffix("...") for action in ui_actions}:
                            resource_labels[key] = value.strip()
            except OSError:
                pass
        prioritized_actions = sorted(
            ui_actions,
            key=lambda action: (
                not action.lower().startswith((
                    "new", "open", "save", "insert", "shape", "zoom", "undo", "redo", "export", "print", "page"
                )),
                action,
            ),
        )[:40]
        source_commit = self._git_output(path, "rev-parse", "HEAD")
        submodules = self._git_output(path, "submodule", "status", "--recursive").splitlines()[:10]

        summary_parts = []
        if sorted_screens:
            summary_parts.append(f"Screens: {', '.join(sorted_screens[:25])}")
        if sorted_routes:
            summary_parts.append(f"Routes: {', '.join(sorted_routes[:25])}")
        if sorted_acc_labels:
            summary_parts.append(f"Accessibility Labels: {', '.join(sorted_acc_labels[:30])}")
        if sorted_test_ids:
            summary_parts.append(f"Test IDs: {', '.join(sorted_test_ids[:30])}")
        if sorted_placeholders:
            summary_parts.append(f"Input Placeholders: {', '.join(sorted_placeholders[:25])}")
        if sorted_button_labels:
            summary_parts.append(f"Actions/Buttons: {', '.join(sorted_button_labels[:25])}")
        if sorted_api_endpoints:
            summary_parts.append(f"API Endpoints: {', '.join(sorted_api_endpoints[:25])}")
        if sorted_form_fields:
            summary_parts.append(f"Form Fields: {', '.join(sorted_form_fields[:20])}")
        if detected_frameworks:
            summary_parts.append(f"Detected Frameworks: {', '.join(sorted(detected_frameworks))}")
        if manifest:
            summary_parts.append(f"Project manifest: {manifest}")
        if source_commit:
            summary_parts.append(f"Inspected source commit: {source_commit}")
        if entrypoints:
            summary_parts.append(f"Application entrypoints: {', '.join(entrypoints)}")
        if prioritized_actions:
            summary_parts.append(
                "Desktop UI actions from source: " + ", ".join(
                    f"{action} ({resource_labels[action.removesuffix('...')]})"
                    if action.removesuffix("...") in resource_labels else action
                    for action in prioritized_actions
                )
            )

        summary_prompt = "\n".join(f"- {part}" for part in summary_parts)

        return {
            "repo_url": sanitize_text(self._repo_url),
            "repo_path": str(path),
            "repo_error": self.last_error,
            "scanned_files": scanned_count,
            "routes": sorted_routes,
            "screens": sorted_screens,
            "test_ids": sorted_test_ids,
            "accessibility_labels": sorted_acc_labels,
            "placeholders": sorted_placeholders,
            "button_labels": sorted_button_labels,
            "api_endpoints": sorted_api_endpoints,
            "form_fields": sorted_form_fields,
            "detected_frameworks": sorted(detected_frameworks),
            "manifest": manifest,
            "entrypoints": entrypoints,
            "ui_actions": prioritized_actions,
            "resource_labels": {
                action: resource_labels[action.removesuffix("...")]
                for action in prioritized_actions
                if action.removesuffix("...") in resource_labels
            },
            "source_commit": source_commit,
            "submodules": submodules,
            "summary_prompt": summary_prompt,
        }

    @staticmethod
    def _canonical_url(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git").replace("git@github.com:", "https://github.com/").lower()

    @staticmethod
    def _git_output(path: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _extract_repo_name(self, url: str) -> str:
        """Derive directory name from repo URL."""
        cleaned = url.rstrip("/").removesuffix(".git")
        parts = cleaned.split("/")
        if len(parts) >= 1 and parts[-1]:
            # e.g. "my-mobile-app" or "track-pilot"
            return re.sub(r'[^a-zA-Z0-9_\-]', '_', parts[-1])
        return "repo"

    def _git_env(self) -> dict[str, str]:
        """Return a git environment with optional GitHub auth kept out of URLs/logs."""
        env = os.environ.copy()
        token = self._github_token()
        if token and "github.com" in self._repo_url.lower():
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
            env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    @staticmethod
    def _github_token() -> str:
        """Use explicit secret variables first, then an existing GitHub CLI login."""
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            return token
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "-h", "github.com"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        return ""

    @staticmethod
    def _sanitize_error(message: str) -> str:
        sanitized = sanitize_text(message.strip())
        for secret_name in ("GH_TOKEN", "GITHUB_TOKEN"):
            secret = os.environ.get(secret_name, "")
            if secret:
                sanitized = sanitized.replace(secret, "<redacted>")
        return sanitized

    def suggest_platform(self, codebase_context: dict[str, Any]) -> str:
        """Suggest a platform based on detected frameworks in the codebase.

        Returns one of: web, android, ios, macos, windows.
        """
        frameworks = set(codebase_context.get("detected_frameworks", []))

        # Electron is the application shell when it coexists with web/mobile
        # source in a monorepo or submodule (for example draw.io desktop).
        if "electron" in frameworks:
            return "macos"
        if frameworks & {"react-native", "flutter", "kotlin-android"}:
            return "android"  # Default to android for cross-platform mobile
        if "swiftui" in frameworks:
            return "ios"
        if frameworks & {"react", "next", "vue", "angular", "django", "fastapi", "flask"}:
            return "web"
        return "web"  # Default

    def generate_suite(
        self,
        codebase_context: dict[str, Any],
        app_name: str = "",
        target_type: str = "web",
        output_dir: str | Path = "testsuites",
    ) -> Path:
        """Generate a .testsuite.md file from codebase analysis results.

        This allows the unified pipeline to automatically create test suites
        for repositories that don't have a pre-existing .testsuite.md.

        Args:
            codebase_context: Output from inspect_codebase().
            app_name: Application name for the suite header.
            target_type: Target platform type (web, mobile, desktop).
            output_dir: Directory to write the generated suite file.

        Returns:
            Path to the generated .testsuite.md file.
        """
        if not app_name:
            manifest = codebase_context.get("manifest", {})
            app_name = manifest.get("name", "")
            if not app_name:
                app_name = self._extract_repo_name(self._repo_url) if self._repo_url else "app"

        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', app_name).lower().strip('-')
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        suite_file = out_path / f"{safe_name}.testsuite.md"

        routes = codebase_context.get("routes", [])
        screens = codebase_context.get("screens", [])
        api_endpoints = codebase_context.get("api_endpoints", [])
        test_ids = codebase_context.get("test_ids", [])
        acc_labels = codebase_context.get("accessibility_labels", [])
        placeholders = codebase_context.get("placeholders", [])
        button_labels = codebase_context.get("button_labels", [])
        form_fields = codebase_context.get("form_fields", [])
        frameworks = codebase_context.get("detected_frameworks", [])

        lines: list[str] = []
        lines.append(f"# {app_name} — AI Testing Instruction Suite\n")
        lines.append("## Purpose\n")
        lines.append(
            f"Auto-generated test suite from codebase analysis of `{self._repo_url}`.\n"
            f"Target: {target_type}. Frameworks: {', '.join(frameworks) or 'generic'}.\n"
        )

        lines.append("# 1. Application Context\n")
        lines.append(f"**Target Type:** `{target_type}`  ")
        if target_type == "web":
            lines.append("**Automation:** Selenium WebDriver  ")
        elif target_type in ("mobile", "desktop"):
            lines.append("**Automation:** Appium  ")
        lines.append("")

        page_num = 1
        section_num = 1

        # Page: UI / Screens
        if routes or screens:
            section_num += 1
            page_num_str = str(section_num)
            lines.append(f"# {page_num_str}. Page {page_num} — Application Navigation\n")
            cat_num = 1

            if routes:
                lines.append(f"## {page_num_str}.{cat_num} Page Load & Routes\n")
                for route in routes[:15]:
                    lines.append(f"- Verify the route `{route}` loads successfully.")
                lines.append("")
                cat_num += 1

            if screens:
                lines.append(f"## {page_num_str}.{cat_num} Screen Navigation\n")
                for screen in screens[:15]:
                    lines.append(f"- Verify the \"{screen}\" screen is accessible and renders correctly.")
                lines.append("")
                cat_num += 1

            page_num += 1

        # Page: UI Elements
        if button_labels or placeholders or form_fields:
            section_num += 1
            page_num_str = str(section_num)
            lines.append(f"# {page_num_str}. Page {page_num} — UI Elements & Forms\n")
            cat_num = 1

            if button_labels:
                lines.append(f"## {page_num_str}.{cat_num} Interactive Elements\n")
                for label in button_labels[:10]:
                    lines.append(f"- Verify the \"{label}\" button/action is visible and clickable.")
                lines.append("")
                cat_num += 1

            if placeholders or form_fields:
                lines.append(f"## {page_num_str}.{cat_num} Form Input Validation\n")
                fields = sorted(set(list(placeholders[:10]) + list(form_fields[:10])))[:15]
                for field in fields:
                    lines.append(f"- Verify the \"{field}\" input field accepts valid data.")
                    lines.append(f"- Verify the \"{field}\" input field rejects empty/invalid input.")
                lines.append("")
                cat_num += 1

            page_num += 1

        # Page: API Tests
        if api_endpoints:
            section_num += 1
            page_num_str = str(section_num)
            lines.append(f"# {page_num_str}. Page {page_num} — API Integration\n")
            lines.append(f"## {page_num_str}.1 API Endpoint Availability\n")
            for endpoint in api_endpoints[:15]:
                lines.append(f"- Verify the API endpoint `{endpoint}` responds without errors.")
            lines.append("")
            page_num += 1

        # Page: Accessibility
        if acc_labels:
            section_num += 1
            page_num_str = str(section_num)
            lines.append(f"# {page_num_str}. Page {page_num} — Accessibility\n")
            lines.append(f"## {page_num_str}.1 Accessibility Labels\n")
            for label in acc_labels[:10]:
                lines.append(f"- Verify the element with accessibility label \"{label}\" is present and reachable.")
            lines.append("")

        content = "\n".join(lines)
        suite_file.write_text(content, encoding="utf-8")
        logger.info(
            "Generated test suite: %s (%d lines, %d pages)",
            suite_file, len(lines), page_num,
        )
        return suite_file
