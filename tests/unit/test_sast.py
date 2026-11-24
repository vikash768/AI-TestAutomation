"""SAST wraps installed scanners and normalises their output; missing tools degrade gracefully."""
import shutil
from pathlib import Path

import pytest

from qaplatform.execute.sast import run_sast

VULN = "import subprocess, hashlib\npassword = 'hunter2'\ndef run(c):\n    subprocess.call(c, shell=True)\ndef h(x):\n    return hashlib.md5(x).hexdigest()\n"


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "app.py").write_text(VULN)
    (tmp_path / "requirements.txt").write_text("requests==2.19.0\n")
    return tmp_path


@pytest.mark.skipif(not shutil.which("bandit"), reason="bandit not installed")
def test_bandit_findings_are_normalised(repo):
    tests, findings, stats = run_sast(repo, lambda *a, **k: None, tools=["bandit"])
    assert stats["sast_tools"]["bandit"] >= 3
    shell = [f for f in findings if "shell=True" in f.title]
    assert shell and shell[0].severity == "high" and shell[0].category == "security"
    assert shell[0].status == "reported" and "app.py:" in shell[0].title
    md5 = [f for f in findings if "MD5" in f.title or "md5" in f.title.lower()]
    assert md5 and md5[0].severity == "high"


@pytest.mark.skipif(not shutil.which("pip-audit"), reason="pip-audit not installed")
def test_pip_audit_reports_one_finding_per_vulnerable_package(repo):
    _, findings, _ = run_sast(repo, lambda *a, **k: None, tools=["pip-audit"])
    reqs = [f for f in findings if "requests" in f.title]
    assert len(reqs) == 1 and "known vulnerabilit" in reqs[0].title  # all CVEs collapsed to one package finding
    assert reqs[0].severity == "high" and "Upgrade to" in reqs[0].description


def test_missing_tool_is_blocked_not_failed(tmp_path):
    tests, findings, stats = run_sast(tmp_path, lambda *a, **k: None, tools=["definitely-not-a-real-tool"])
    # unknown tool name is ignored; a known-but-absent tool records a blocked test
    assert findings == []


def test_absent_known_tool_records_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    tests, findings, stats = run_sast(tmp_path, lambda *a, **k: None, tools=["bandit", "semgrep", "gitleaks"])
    assert findings == [] and all(t.status == "blocked" for t in tests)
    assert "not installed" in tests[0].observed
