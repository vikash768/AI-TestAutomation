from qaplatform.explore.crawler import ObservedElement, ObservedPage
from qaplatform.explore.safety import classify_action
from qaplatform.ingest.js_parser import CodeElement
from qaplatform.reconcile.matcher import normalize, reconcile


def test_classify_action_rules():
    assert classify_action("Log out", {}, {}) == "session_ending"
    assert classify_action("Delete user", {}, {}) == "destructive"
    assert classify_action("Save changes", {}, {}) == "mutating"
    assert classify_action("Add User", {}, {"opens": ["drawer"], "api_calls": []}) == "safe"
    assert classify_action("Reactivate", {}, {"api_calls": [{"method": "POST", "path": "/x"}]}) == "mutating"
    assert classify_action("Anything", {}, {"api_calls": [{"method": "DELETE", "path": "/x"}]}) == "destructive"
    assert classify_action("Download for Mac", {"href": "https://x/y/TrackPilot.dmg"}, {}) == "download"
    assert classify_action("Dashboard", {"href": "/dashboard"}, {}) == "safe"
    assert classify_action("Go", {"type": "submit"}, {}) == "mutating"


def test_normalize():
    assert normalize("Actions ▾") == "actions"
    assert normalize("  Save   Changes… ") == "save changes"
    assert normalize("Projects & Tasks") == "projects & tasks"


def _code(label, kind, route, href=None, **kw):
    return CodeElement(file="f.jsx", line=1, tag="button", kind=kind, label=label, route=route, href=href, key=f"{route}|{kind}|{label}", **kw)


def _obs(name, role, selector, href=None, visible=True):
    return ObservedElement(selector=selector, role=role, name=name, tag="button" if role == "button" else "a", href=href, visible=visible)


def test_reconcile_matches_by_label_href_and_layout():
    code = [
        _code("Dashboard", "link", "(layout)", href="/dashboard"),
        _code("Save User", "button", "/users"),
        _code("Actions ▾", "button", "/users", repeated=True),
        _code("Ghost", "button", "/users"),
        _code("Close", "button", "/users", conditional=True),
    ]
    page = ObservedPage(route="/users", url="https://app/users", protected=True, elements=[
        _obs("Dashboard", "link", "nav a:nth-of-type(1)", href="https://app/dashboard"),
        _obs("Save User", "button", "form button"),
        _obs("Actions", "button", "tr:nth-of-type(1) button"),
        _obs("Actions", "button", "tr:nth-of-type(2) button"),
        _obs("Extra thing", "button", "div button"),
    ])
    res = reconcile(code, [page], ["/users"], "https://app")
    found = {m.code.label: m for m in res.matches if m.observed}
    assert set(found) == {"Dashboard", "Save User", "Actions ▾"}
    assert "href=" in found["Dashboard"].reason
    assert {c.label for c in res.unmatched_code} == {"Ghost", "Close"}
    assert [o.name for _, o in res.unmatched_observed] == ["Extra thing"]  # second "Actions" row is a twin of the repeated menu
    assert res.stats["code_interactive"] == 5 and res.stats["matched"] == 3
    # unconditional coverage ignores the conditional Close and the repeated menu
    assert res.coverage_unconditional == 2 / 3


def test_templated_labels_and_twins():
    from qaplatform.reconcile.matcher import label_pattern
    assert label_pattern("Download for {label}").match("download for mac macos 12 or later apple silicon")
    assert label_pattern("${label}: ${formatRangeLabel(from, to)}") is None  # too little literal text
    code = [_code("Download for {label}", "link", "/"), _code("Dashboard", "link", "(layout)", href="/dashboard")]
    pages = [
        ObservedPage(route="/", url="https://app/", elements=[_obs("Download for Mac macOS 12", "link", "a:nth-of-type(1)", href="https://x/a.dmg"), _obs("Download for Windows 10", "link", "a:nth-of-type(2)", href="https://x/b.exe")]),
        ObservedPage(route="/users", url="https://app/users", protected=True, elements=[_obs("Dashboard", "link", "nav a", href="https://app/dashboard")]),
        ObservedPage(route="/live", url="https://app/live", protected=True, elements=[_obs("Dashboard", "link", "nav a", href="https://app/dashboard")]),
    ]
    res = reconcile(code, pages, ["/users", "/live"], "https://app")
    assert res.stats["matched"] == 2
    assert [o.name for _, o in res.unmatched_observed] == ["Download for Windows 10"]
