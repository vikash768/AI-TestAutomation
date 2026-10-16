"""
Repository Codebase Reader and Inspector.

Reads codebase from a repository URL (or local path) specified in .env (APP_REPO_URL),
extracts routes, component/screen names, testIDs, accessibility labels, and form fields,
and provides grounded context to the AI test generator.
"""

from __future__ import annotations

import logging
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
}

# Directories to always skip during scanning
IGNORED_DIRS = {
    "node_modules", ".git", ".github", "dist", "build", "coverage",
    ".next", ".nuxt", "Pods", ".gradle", "bin", "obj", ".idea", ".vscode",
    "venv", ".venv", "__pycache__", ".cache", "vendor",
}


class RepoReader:
    """Reads and inspects code from a git repository or local codebase."""

    def __init__(self, repo_url: str = "", cache_dir: str | Path | None = None):
        self._repo_url = (repo_url or os.environ.get("APP_REPO_URL", "")).strip()
        self._cache_dir = Path(cache_dir or "artifacts/repos")
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

        # Check if repo_url is an existing local directory
        local_path = Path(self._repo_url).expanduser()
        if local_path.is_dir():
            logger.info("Using local codebase directory: %s", local_path)
            return local_path

        # Otherwise treat as a git URL and clone to cache_dir
        repo_name = self._extract_repo_name(self._repo_url)
        dest_dir = self._cache_dir / repo_name

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
                except Exception as e:
                    logger.debug(
                        "Non-fatal error updating repository %s: %s",
                        dest_dir,
                        sanitize_text(str(e)),
                    )
                return dest_dir

            logger.info(
                "Cloning repository from %s into %s (depth=1)...",
                sanitize_text(self._repo_url),
                dest_dir,
            )
            clone_command = ["git", "clone", "--depth", "1"]
            if self._repo_ref:
                clone_command.extend(["--branch", self._repo_ref])
            clone_command.extend([self._repo_url, str(dest_dir)])
            result = subprocess.run(
                clone_command,
                capture_output=True,
                text=True,
                timeout=45,
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

        # Regular expressions for common UI elements
        re_test_id = re.compile(r'(?:testID|data-testid)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_acc_label = re.compile(r'(?:accessibilityLabel|aria-label|contentDescription)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_placeholder = re.compile(r'placeholder\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_route_path = re.compile(r'(?:path|route)\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)
        re_screen_name = re.compile(r'<Screen\s+[^>]*name\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        re_button_title = re.compile(r'(?:title|text)\s*=\s*["\']([A-Z][a-zA-Z0-9\s]{2,25})["\']')

        scanned_count = 0
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

                except Exception:
                    continue

        logger.info(
            "Scanned %d files in %s: %d testIDs, %d acc labels, %d placeholders, %d screens, %d routes",
            scanned_count, path, len(test_ids), len(accessibility_labels), len(placeholders), len(screens), len(routes)
        )

        # Sort and limit for prompt compactness
        sorted_routes = sorted(routes)[:40]
        sorted_screens = sorted(screens)[:40]
        sorted_test_ids = sorted(test_ids)[:50]
        sorted_acc_labels = sorted(accessibility_labels)[:50]
        sorted_placeholders = sorted(placeholders)[:40]
        sorted_button_labels = sorted(button_labels)[:40]

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
            "summary_prompt": summary_prompt,
        }

    def _extract_repo_name(self, url: str) -> str:
        """Derive directory name from repo URL."""
        cleaned = url.rstrip("/").removesuffix(".git")
        parts = cleaned.split("/")
        if len(parts) >= 1 and parts[-1]:
            # e.g. "Ijustpaid-mobileApp" or "track-pilot"
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
