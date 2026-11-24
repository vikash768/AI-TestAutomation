"""Static analysis of the codebase (Tier 2). Wraps established, maintained scanners rather than
reinventing taint analysis — Semgrep (polyglot), Bandit (Python), pip-audit (Python deps) and
gitleaks (secrets) — and normalises every result into the platform's FindingRecord shape so they
flow through the same report and confidence handling as everything else.

Each tool is optional: if it is not installed, its check is recorded as blocked, not failed. Nothing
here executes the target's code — these read source only (SCA resolves declared dependencies).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint

logger = logging.getLogger(__name__)
CATEGORY = "security"
TIMEOUT = 240  # a scan that runs longer than this is cut off rather than hanging the run

SEVERITY = {  # tool severity -> ours
    "CRITICAL": "critical", "ERROR": "high", "HIGH": "high", "WARNING": "medium", "MEDIUM": "medium",
    "INFO": "low", "LOW": "low", "WARN": "medium",
}


def _rel(path: str, repo: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(repo.resolve()))
    except Exception:
        return path


def _run_json(cmd: list[str], cwd: Path | None = None) -> dict | list | None:
    """Run a scanner and parse its JSON stdout. Most of these exit non-zero WHEN they find issues,
    so a non-zero code is not an error; only a crash or unparseable output is."""
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"{cmd[0]} {type(exc).__name__}") from exc
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def _finding(tool: str, rule: str, rel: str, line: int, sev: str, title: str, desc: str, ref: str = "", conf: float = 0.7) -> FindingRecord:
    where = f"{rel}:{line}" if line else rel
    f = FindingRecord(
        fingerprint=fingerprint("sast", tool, rule, rel, str(line)), route="(codebase)", category=CATEGORY,
        severity=SEVERITY.get(sev.upper(), "medium"), title=f"{title} ({where})", description=desc,
        expected="No known security weakness at this location", observed=f"{tool} rule {rule}", standard_ref=ref,
        confidence=conf, reproduced_n=1, reproduced_m=1,
        # Static findings are deterministic but can be false positives; a human confirms before acting.
        status="reported" if SEVERITY.get(sev.upper()) in ("critical", "high") else "needs_review",
        evidence=[{"type": "code", "messages": [where] + ([ref] if ref else [])}],
    )
    return f


# -- individual scanners --------------------------------------------------------------------------

def _bandit(exe: str, repo: Path) -> list[FindingRecord]:
    data = _run_json([exe, "-r", str(repo), "-f", "json", "-q"])
    out = []
    for r in (data or {}).get("results", []):
        cwe = (r.get("issue_cwe") or {}).get("link") or f"CWE-{(r.get('issue_cwe') or {}).get('id', '')}"
        out.append(_finding("bandit", r.get("test_id", "?"), _rel(r.get("filename", ""), repo), r.get("line_number", 0),
                            r.get("issue_severity", "LOW"), r.get("issue_text", "")[:120], r.get("issue_text", ""),
                            ref=cwe, conf=0.6 if r.get("issue_confidence") == "LOW" else 0.75))
    return out


def _semgrep(exe: str, repo: Path) -> list[FindingRecord]:
    cfg = __import__("os").environ.get("SEMGREP_CONFIG", "auto")  # pin a ruleset to avoid the registry if needed
    data = _run_json([exe, "scan", "--config", cfg, "--json", "--quiet", "--timeout", "0", str(repo)])
    out = []
    for r in (data or {}).get("results", []):
        extra = r.get("extra", {})
        meta = extra.get("metadata", {})
        ref = ", ".join(x for x in [_first(meta.get("cwe")), _first(meta.get("owasp"))] if x)
        out.append(_finding("semgrep", r.get("check_id", "?").split(".")[-1], _rel(r.get("path", ""), repo),
                            (r.get("start") or {}).get("line", 0), extra.get("severity", "WARNING"),
                            (extra.get("message") or "")[:120], extra.get("message", ""), ref=ref, conf=0.75))
    return out


def _pip_audit(exe: str, repo: Path) -> list[FindingRecord]:
    reqs = [p for p in repo.rglob("requirements*.txt") if "node_modules" not in p.parts][:5]
    out = []
    for req in reqs:
        data = _run_json([exe, "-f", "json", "-r", str(req), "--progress-spinner", "off"])
        deps = data.get("dependencies", []) if isinstance(data, dict) else (data or [])
        for d in deps:
            vulns = d.get("vulns", [])
            if not vulns:
                continue
            # One finding per vulnerable package (not per CVE — an old package has many): list the ids.
            ids = ", ".join(dict.fromkeys(a for v in vulns for a in (v.get("aliases") or [v.get("id", "")])[:1]))
            fixes = [fv for v in vulns for fv in (v.get("fix_versions") or [])]
            fix = f"Upgrade to {sorted(set(fixes))[-1]}." if fixes else "No fixed version is published yet."
            out.append(_finding("pip-audit", f"{d.get('name')}@{d.get('version')}", _rel(str(req), repo), 0, "HIGH",
                                f"{d.get('name')} {d.get('version')} has {len(vulns)} known vulnerabilit{'y' if len(vulns) == 1 else 'ies'}",
                                f"{(vulns[0].get('description') or '')[:260]} {fix}", ref=ids, conf=0.9))
    return out


def _gitleaks(exe: str, repo: Path) -> list[FindingRecord]:
    rpt = repo / ".qa-gitleaks.json"
    try:
        # gitleaks v8: "dir" scans a directory (the old "detect --source" is gone).
        subprocess.run([exe, "dir", str(repo), "--report-format", "json", "--report-path", str(rpt), "--no-banner", "--redact", "--exit-code", "0"],
                       capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(rpt.read_text()) if rpt.exists() else []
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"gitleaks {type(exc).__name__}") from exc
    finally:
        rpt.unlink(missing_ok=True)
    out = []
    for r in data or []:
        out.append(_finding("gitleaks", r.get("RuleID", "secret"), _rel(r.get("File", ""), repo), r.get("StartLine", 0),
                            "CRITICAL", f"Possible leaked secret: {r.get('Description', 'secret')}",
                            f"A secret matching '{r.get('RuleID')}' is committed in the repository. Rotate it and remove it from history.",
                            ref="CWE-798 Use of Hard-coded Credentials", conf=0.8))
    return out


def _trivy(exe: str, repo: Path) -> list[FindingRecord]:
    # Cross-language dependency CVEs (npm, pip, go, gem, cargo, ...) from lockfiles/manifests.
    data = _run_json([exe, "fs", "--quiet", "--format", "json", "--scanners", "vuln", str(repo)])
    out = []
    for res in (data or {}).get("Results", []) or []:
        by_pkg: dict = {}
        for v in res.get("Vulnerabilities", []) or []:
            by_pkg.setdefault((v.get("PkgName"), v.get("InstalledVersion")), []).append(v)
        for (pkg, ver), vulns in by_pkg.items():
            ids = ", ".join(dict.fromkeys(v.get("VulnerabilityID", "") for v in vulns))[:120]
            fix = next((v.get("FixedVersion") for v in vulns if v.get("FixedVersion")), "")
            sev = max((v.get("Severity", "LOW") for v in vulns), key=lambda s: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(s) if s in ("LOW", "MEDIUM", "HIGH", "CRITICAL") else 0)
            out.append(_finding("trivy", f"{pkg}@{ver}", _rel(res.get("Target", ""), repo), 0, sev,
                                f"{pkg} {ver} has {len(vulns)} known vulnerabilit{'y' if len(vulns) == 1 else 'ies'}",
                                f"{(vulns[0].get('Title') or '')[:220]}" + (f" Upgrade to {fix}." if fix else ""), ref=ids, conf=0.9))
    return out


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


_RUNNERS = {"semgrep": _semgrep, "bandit": _bandit, "pip-audit": _pip_audit, "gitleaks": _gitleaks, "trivy": _trivy}


def run_sast(repo_path, events, tools: list[str] | None = None) -> tuple[list[TestRecord], list[FindingRecord], dict]:
    repo = Path(repo_path)
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    ran: dict[str, int] = {}
    for name in (tools or list(_RUNNERS)):
        fn = _RUNNERS.get(name)
        if fn is None:
            continue
        exe = shutil.which(name)
        key = f"sast-{name}"
        if not exe:
            tests.append(TestRecord(test_key=key, name=f"Static analysis: {name}", technique="sast", category=CATEGORY, route="(codebase)",
                                    expected=f"{name} scans the source", observed=f"{name} is not installed; skipped", status="blocked"))
            continue
        events("sast", f"Static analysis: running {name}", 15)
        try:
            fs = fn(exe, repo)
        except Exception as exc:
            logger.warning("sast %s failed: %s", name, exc)
            tests.append(TestRecord(test_key=key, name=f"Static analysis: {name}", technique="sast", category=CATEGORY, route="(codebase)",
                                    expected=f"{name} scans the source", observed=f"{name} failed: {type(exc).__name__}: {str(exc)[:160]}", status="blocked"))
            continue
        findings.extend(fs)
        ran[name] = len(fs)
        tests.append(TestRecord(test_key=key, name=f"Static analysis: {name}", technique="sast", category=CATEGORY, route="(codebase)",
                                expected=f"{name} finds no security weakness", observed=f"{name}: {len(fs)} finding(s)",
                                status="fail" if fs else "pass", reproduced_n=1, reproduced_m=1))
    # dedup exact repeats (same tool+rule+file+line fingerprint)
    seen, uniq = set(), []
    for f in findings:
        if f.fingerprint in seen:
            continue
        seen.add(f.fingerprint)
        uniq.append(f)
    # Never imply the code is secure: state what ran and that a clean result is not a guarantee.
    scanned = ", ".join(f"{k} ({v})" for k, v in ran.items()) or "no scanners available"
    uniq.append(FindingRecord(
        fingerprint=fingerprint("sast", "coverage"), route="(codebase)", category="information", severity="low",
        title="Static-analysis coverage (not a security guarantee)",
        description=f"Scanned with: {scanned}. These tools find known weakness patterns and vulnerable dependencies. "
                    "A clean result does not prove the code is secure — it was not exhaustively tested, and logic/business-rule flaws are out of scope.",
        expected="", observed=f"{len(uniq)} finding(s)", confidence=1.0, reproduced_n=1, reproduced_m=1, status="reported", bucket="info"))
    events("sast", f"Static analysis: {len(uniq) - 1} finding(s) from {len(ran)} tool(s)", 18)
    return tests, uniq, {"sast_tools": ran}
