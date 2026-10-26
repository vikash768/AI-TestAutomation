"""Fetch the codebase and detect its framework."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoInfo:
    path: Path
    commit_sha: str = ""
    framework: str = "unknown"
    router: str = ""
    language: str = "javascript"
    package: dict = field(default_factory=dict)


def _with_token(url: str, token: str | None) -> str:
    if not token or not url.startswith("https://"):
        return url
    if "@" in url.split("//", 1)[1].split("/", 1)[0]:
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def fetch_repo(url_or_path: str, dest: Path, token: str | None = None, branch: str | None = None) -> RepoInfo:
    """Clone a git URL (shallow) or use a local directory. Never logs the token."""
    src = Path(url_or_path).expanduser()
    if src.exists() and src.is_dir():
        info = RepoInfo(path=src.resolve())
        try:
            sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
            info.commit_sha = sha.stdout.strip() if sha.returncode == 0 else ""
        except Exception:
            pass
        return _detect(info)

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base_cmd = ["git", "clone", "--depth", "1"]
    if branch:
        base_cmd += ["--branch", branch]
    # Public repositories clone anonymously; the token is only sent when that fails.
    attempts = [url_or_path] + ([_with_token(url_or_path, token)] if token and url_or_path.startswith("https://") else [])
    last_err = ""
    for candidate in attempts:
        if dest.exists():
            shutil.rmtree(dest)
        proc = subprocess.run(base_cmd + [candidate, str(dest)], capture_output=True, text=True, timeout=600, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if proc.returncode == 0:
            break
        last_err = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", proc.stderr).strip()[:500]
    else:
        hint = ""
        if re.search(r"403|404|not found|could not read Username|Authentication failed|access .* not granted", last_err, re.I):
            hint = (" — the repository is private or the token cannot read it. Use a GitHub token with read access to this "
                    "repository (fine-grained: Contents read-only on the repo; classic: 'repo' scope, SSO-authorised for the org), "
                    "set as GITHUB_TOKEN or in the form's token field.")
        raise RuntimeError(f"git clone failed: {last_err}{hint}")
    sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
    info = RepoInfo(path=dest.resolve(), commit_sha=sha.stdout.strip())
    return _detect(info)


def _detect(info: RepoInfo) -> RepoInfo:
    pkg_path = info.path / "package.json"
    if pkg_path.exists():
        try:
            info.package = json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:
            info.package = {}
        deps = {**info.package.get("dependencies", {}), **info.package.get("devDependencies", {})}
        if "next" in deps:
            info.framework = "nextjs"
        elif "react" in deps:
            info.framework = "react"
        elif "vue" in deps:
            info.framework = "vue"
        elif "@angular/core" in deps:
            info.framework = "angular"
        elif "svelte" in deps:
            info.framework = "svelte"
        else:
            info.framework = "javascript"
        if "react-router-dom" in deps or "react-router" in deps:
            info.router = "react-router"
        elif "next" in deps:
            info.router = "next-file-routes"
        if "typescript" in deps or any(info.path.glob("tsconfig*.json")):
            info.language = "typescript"
    elif (info.path / "manage.py").exists() or any(info.path.glob("**/settings.py")):
        info.framework = "django"
        info.language = "python"
    return info
