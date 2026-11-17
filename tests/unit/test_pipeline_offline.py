"""Page-level checks and the HTML report, without a browser or network."""
from pathlib import Path

from qaplatform.config import Settings
from qaplatform.execute.smoke import ComponentRecord, guard_tests, page_tests
from qaplatform.explore.crawler import GuardResult, ObservedPage
from qaplatform.report.html import write_report


def test_guard_and_page_tests_produce_findings(tmp_path):
    guards = [GuardResult("/users", "https://a/users", "https://a/login", True, True, True), GuardResult("/audit", "https://a/audit", "https://a/audit", False, False, True)]
    t, f = guard_tests(guards)
    assert [x.status for x in t] == ["pass", "fail"]
    assert f[0].severity == "critical" and "WSTG" in f[0].standard_ref

    page = ObservedPage(route="/users", url="https://a/users", title="Users", status_code=200, load_ms=800, protected=True,
                        console_errors=["TypeError: x is undefined"], failed_requests=[{"url": "https://a/api/x", "method": "GET", "status": 500, "error": "", "type": "fetch"}],
                        state={"bodyTextLength": 500, "rootEmpty": False},
                        viewports={"mobile": {"width": 360, "height": 780, "docW": 360, "scrollW": 520, "horizontalOverflow": True, "overflowing": [{"tag": "table", "id": None, "text": "wide", "bbox": [0, 0, 520, 40]}], "overlaps": [], "smallTargets": [{"tag": "button", "text": "x", "bbox": [0, 0, 18, 18]}], "clipped": [], "screenshot": str(tmp_path / "m.png")},
                                   "desktop": {"width": 1440, "height": 900, "docW": 1440, "scrollW": 1440, "horizontalOverflow": False, "overflowing": [], "overlaps": [], "smallTargets": [], "clipped": [], "screenshot": str(tmp_path / "d.png")}})
    t2, f2 = page_tests([page], Settings())
    titles = {x.title for x in f2}
    assert any("Console errors" in s for s in titles) and any("Server error" in s for s in titles)
    assert any("Horizontal overflow" in s and "mobile" in s for s in titles) and any("Tap targets" in s for s in titles)
    assert not any("desktop" in s for s in titles)
    refs = {x.standard_ref for x in f2}
    assert "WCAG 2.2 1.4.10 Reflow" in refs and "WCAG 2.2 2.5.8 Target Size (Minimum)" in refs

    comps = [ComponentRecord(key="k", route="/users", kind="button", label="Save", source="both", status="skipped", action_class="mutating")]
    out = write_report(tmp_path, "run1", {"live_url": "https://a", "repo_url": "x", "commit_sha": "abc", "framework": "react", "finished_at": "now"}, {"coverage": 0.5, "coverage_unconditional": 0.6, "code_interactive": 2, "matched": 1}, [page], comps, t + t2, f + f2)
    html = out.read_text()
    assert "Horizontal overflow" in html and "Save" in html and "50%" in html
