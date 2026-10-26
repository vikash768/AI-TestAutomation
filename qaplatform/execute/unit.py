"""Run the codebase's own test suite and report it as the 'unit' category.

This runs the project's declared test command (npm test, pytest, go test, …)
inside the worker, with a timeout. It executes repository code, so it runs
only when explicitly enabled, and only ever the code the operator handed us to
test. When the toolchain or a required service (e.g. a database the suite
needs) is absent, it records a clear 'blocked' rather than a failure.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint

CATEGORY = "unit"
COUNT_RE = [
    re.compile(r"Tests:\s+(?:(\d+) failed,\s+)?(\d+) passed,\s+(\d+) total"),   # jest/vitest
    re.compile(r"#\s*pass\s+(\d+).*?#\s*fail\s+(\d+)", re.S),                    # node --test TAP summary
    re.compile(r"(\d+) passing(?:\D+(\d+) failing)?", re.S),                     # mocha
    re.compile(r"(\d+) passed(?:,\s*(\d+) failed)?", re.S),                      # pytest-ish
]


def _detect(repo: Path) -> tuple[str, list[str]] | None:
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except Exception:
            scripts = {}
        if "test" in scripts and shutil.which("npm"):
            return "node", ["npm", "test"]
    if (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists() or list(repo.glob("test_*.py")) or list(repo.glob("tests/**/*.py")):
        if shutil.which("pytest"):
            return "pytest", ["pytest", "-q"]
    if (repo / "go.mod").exists() and shutil.which("go"):
        return "go", ["go", "test", "./..."]
    return None


def _parse_counts(output: str) -> tuple[int, int] | None:
    for rx in COUNT_RE:
        m = rx.search(output)
        if m:
            g = [int(x) for x in m.groups() if x and x.isdigit()]
            if rx is COUNT_RE[0]:
                failed = int(m.group(1) or 0)
                passed = int(m.group(2) or 0)
                return passed, failed
            if len(g) >= 2:
                return g[0], g[1]
            if g:
                return g[0], 0
    return None


def run_unit(repo_path: Path, project: str, events, env: dict | None = None, timeout: int = 600, install: bool = True) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    repo = Path(repo_path)
    detected = _detect(repo)
    if detected is None:
        tests.append(TestRecord(test_key="unit|suite", name=f"{project} unit tests", technique="unit", category=CATEGORY, route="(codebase)",
                                expected="The codebase's own test suite runs and passes",
                                observed="no runnable test command found, or its toolchain (node/npm, pytest, go) is not installed here", status="blocked"))
        return tests, findings
    kind, cmd = detected
    runenv = {**os.environ, **(env or {})}

    if kind == "node" and install and not (repo / "node_modules").exists():
        events("unit", f"{project}: installing dependencies", 90)
        inst = subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=repo, capture_output=True, text=True, timeout=timeout, env=runenv)
        if inst.returncode != 0:
            tests.append(TestRecord(test_key="unit|install", name=f"{project} install dependencies", technique="unit", category=CATEGORY, route="(codebase)",
                                    expected="Dependencies install so the suite can run", observed=f"npm install failed: {inst.stderr.strip()[-300:]}", status="blocked"))
            return tests, findings

    events("unit", f"{project}: running {' '.join(cmd)}", 91)
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=runenv)
    except subprocess.TimeoutExpired:
        tests.append(TestRecord(test_key="unit|suite", name=f"{project} unit tests", technique="unit", category=CATEGORY, route="(codebase)",
                                expected="The suite completes within the time budget", observed=f"timed out after {timeout}s", status="blocked"))
        return tests, findings
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    counts = _parse_counts(out)
    needs_db = re.search(r"(DATABASE_URL|ECONNREFUSED|could not connect|database .* does not exist|connection refused)", out, re.I)

    if counts is None and proc.returncode != 0 and needs_db:
        tests.append(TestRecord(test_key="unit|suite", name=f"{project} unit tests", technique="unit", category=CATEGORY, route="(codebase)",
                                expected="The suite runs and passes", observed="suite needs a service that is not available here (e.g. DATABASE_URL); not a code failure", status="blocked"))
        return tests, findings

    passed, failed = counts if counts else (0, 0 if proc.returncode == 0 else 1)
    total = passed + failed
    status = "pass" if proc.returncode == 0 and failed == 0 else "fail"
    tail = "\n".join(l for l in out.splitlines() if re.search(r"(fail|error|✗|✖|not ok|assert)", l, re.I))[:1500]
    t = TestRecord(test_key="unit|suite", name=f"{project} unit tests ({kind})", technique="unit", category=CATEGORY, route="(codebase)",
                   steps=[{"type": "RUN", "value": " ".join(cmd)}], expected="The codebase's own test suite passes",
                   observed=f"{passed} passed, {failed} failed" + (f" of {total}" if total else "") + f"; exit {proc.returncode}", status=status, reproduced_n=1, reproduced_m=1)
    tests.append(t)
    if status == "fail":
        findings.append(FindingRecord(fingerprint=fingerprint("unit", project), route="(codebase)", category=CATEGORY, severity="high" if failed else "medium",
                                      title=f"{project} unit tests fail ({failed or '?'} failing)", description=tail or out[-800:],
                                      expected="The codebase's own test suite passes", observed=t.observed,
                                      standard_ref="ISTQB component (unit) testing", confidence=0.95, reproduced_n=1, reproduced_m=1, status="reported"))
    return tests, findings
