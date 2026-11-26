"""Fetch the codebase and detect its framework."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass
class RepoInfo:
    path: Path
    commit_sha: str = ""
    framework: str = "unknown"
    router: str = ""
    language: str = "javascript"
    package: dict = field(default_factory=dict)


def _https_scope(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or parts.username is not None:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_repo(url_or_path: str, dest: Path, token: str | list[str] | None = None, branch: str | None = None) -> RepoInfo:
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
    # Try public repositories anonymously before using tokens supplied for this run.
    tokens = [t.strip() for t in ([token] if isinstance(token, str) else (token or [])) if t and t.strip()]
    authorizations = {t: base64.b64encode(f"x-access-token:{t}".encode()).decode("ascii") for t in tokens}
    scope = _https_scope(url_or_path)
    attempts = [None, *(authorizations.values() if scope else ())]
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_TRACE_REDACT": "1"}
    errors: list[str] = []
    for authorization in attempts:
        if dest.exists():
            shutil.rmtree(dest)
        env = git_env.copy()
        if authorization is not None:
            # Command-scoped Git config keeps credentials out of arguments and .git/config.
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            env[f"GIT_CONFIG_KEY_{count}"] = f"http.{scope}.extraHeader"
            env[f"GIT_CONFIG_VALUE_{count}"] = f"Authorization: Basic {authorization}"
            env["GIT_CONFIG_COUNT"] = str(count + 1)
        try:
            proc = subprocess.run(
                base_cmd + ["--", url_or_path, str(dest)], capture_output=True, text=True, timeout=600, env=env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("git clone timed out after 600 seconds") from None
        if proc.returncode == 0:
            break
        error = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", proc.stderr)
        for raw, encoded in authorizations.items():
            error = error.replace(raw, "***").replace(encoded, "***")
        errors.append(error.strip()[:500])
    else:
        # report the most telling answer, not just the last token's: "no access to this repo" beats "this token is invalid"
        last_err = next((e for e in errors if re.search(r"403|404|not found|access .* not granted", e, re.I)), errors[-1])
        if len(attempts) > 1:
            last_err = f"tried {len(attempts) - 1} token(s) and an anonymous clone; {last_err}"
        hint = ""
        if re.search(r"403|404|not found|could not read Username|Authentication failed|access .* not granted", last_err, re.I):
            hint = (" — check the repository URL and supply a token with read access in the run form "
                    "or with --github-token.")
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
