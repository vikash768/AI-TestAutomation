from types import SimpleNamespace

from qaplatform.app.checks import ActiveChecks, screen_checks, tap_results
from qaplatform.app.device import Snapshot
from qaplatform.app.explore import AppExplorer, Screen, Tap
from qaplatform.explore.crawler import ObservedElement


def _control(name, role="button", *, window=1, bounds=None, index=1, dialog=False, tag=None):
    return ObservedElement(
        selector=f"~{name}", name=name, role=role, tag=tag or role.title(),
        bbox=bounds or [120, 210, 30, 20], in_dialog=dialog,
        path_hint=f"/Application[1]/Window[{window}]/Control[{index}]",
    )


def test_macos_geometry_scopes_overlap_and_clipping_to_the_own_window():
    a = _control("Open", bounds=[130, 220, 53, 28], index=1)
    b = _control("Details", bounds=[148, 222, 39, 33], index=2)
    clipped = _control("Clipped vertically", bounds=[250, 363, 50, 30], index=3)
    scroll = _control("Scroll cut", bounds=[120, 355, 30, 60], index=4)
    secondary = _control("Other window", bounds=[156, 222, 39, 28], window=2)
    dialog = _control("Modal", bounds=[153, 221, 39, 28], dialog=True, index=5)
    elements = [a, b, clipped, scroll, secondary, dialog]
    snap = Snapshot(elements=elements, width=300, height=180, origin=(100, 200),
                    element_windows={e.path_hint: (100, 200, 300, 180) for e in elements},
                    scrollable_elements={scroll.path_hint})
    tests, findings = screen_checks([Screen(snap.signature, snap, route="Details")], 0, "macos")

    by_check = {test.test_key.rsplit("|", 2)[0]: test for test in tests}
    assert "target" not in by_check
    assert by_check["desktop-clipping"].status == "needs_review"
    assert "Clipped vertically" in by_check["desktop-clipping"].observed
    assert "Scroll cut" not in by_check["desktop-clipping"].observed
    assert by_check["desktop-overlap"].status == "needs_review"
    assert "1 possible overlap" in by_check["desktop-overlap"].observed
    assert "Other window" not in by_check["desktop-overlap"].observed
    assert "Modal" not in by_check["desktop-overlap"].observed
    assert len(findings) == 2 and all(f.status == "needs_review" and f.viewport == "window" for f in findings)


def test_macos_menus_check_accessible_names_and_mark_empty_ax_as_blocked():
    menu_item = _control("", "menuitem", tag="MenuItem")
    menu = Screen("menu", Snapshot(texts=["Tools"], elements=[menu_item]), route="Tools", kind="menu")
    empty = Screen("empty", Snapshot(texts=["Help"]), route="Help", kind="menu")
    tests, findings = screen_checks([menu, empty], 0, "macos")
    assert any(t.route == "Tools" and t.technique == "accessibility" and t.status == "fail" for t in tests)
    assert any(t.route == "Help" and t.technique == "page_health" and t.status == "blocked" for t in tests)
    assert any("without an accessible name" in issue.title for issue in findings)
    assert not any(test.technique == "desktop-layout" for test in tests)


def test_macos_tap_reports_unreached_and_no_visible_response_without_false_failures():
    explorer = AppExplorer(SimpleNamespace(platform="macos"), lambda *_args, **_kw: None)
    explorer.screens = [Screen("home", Snapshot(), route="Home")]
    explorer.taps = [
        Tap("home", _control("Save"), action_class="mutating", result="skipped", detail="Writes saved data"),
        Tap("home", _control("Open preferences"), result="not_reached", detail="Could not restore the parent screen"),
        Tap("home", _control("Show details"), result="same"),
        Tap("home", _control("Pinned tab", "tab"), result="same"),
        Tap("home", _control("Help"), result="foreign", detail="Target app lost foreground"),
    ]
    tests, findings = tap_results(explorer, {})
    assert [test.status for test in tests] == ["blocked", "blocked", "needs_review", "pass", "blocked"]
    assert tests[0].observed == "Writes saved data"
    assert tests[1].observed == "Could not restore the parent screen"
    assert len(findings) == 1 and findings[0].status == "needs_review"


class SearchDevice:
    platform = "macos"

    def __init__(self, snap, fail_restore=False):
        self.snap = snap
        self.fail_restore = fail_restore
        self.value = {box.name: "original query" for box in snap.elements}
        self.calls = []

    def read_value(self, box, *, strict=False):
        assert strict
        if box.name == "Unreadable":
            raise RuntimeError("field value is unavailable")
        return self.value[box.name]

    def type_into(self, box, value):
        self.calls.append((box.name, value))
        if self.fail_restore and value == "original query":
            return False
        self.value[box.name] = value[:55]
        return True

    def snapshot(self, _settle):
        return self.snap

    def app_state(self):
        return 4


def test_macos_search_probes_restore_exact_value_and_hold_document_fields():
    elements = [_control("Notes", "textbox"), _control("Unreadable", "searchbox", index=2),
                _control("Search", "searchbox", index=3)]
    screen = Screen("detail", Snapshot(elements=elements), route="Details")
    device = SearchDevice(screen.snap)
    explorer = AppExplorer(device, lambda *_args, **_kw: None)
    explorer.screens = [screen]
    checks = ActiveChecks(device, explorer, lambda *_args, **_kw: None, "Details")
    checks.inputs()

    assert [test.status for test in checks.tests] == ["blocked", "blocked", "pass"]
    assert device.calls == [("Search", "A" * 300), ("Search", "original query")]
    assert device.value["Search"] == "original query"
    assert checks.tests[-1].cleanup == "Original value restored and verified"


def test_macos_search_probes_stop_when_original_value_cannot_be_restored():
    screen = Screen("detail", Snapshot(elements=[_control("Search", "searchbox"), _control("Filter", "searchbox", index=2)]), route="Details")
    device = SearchDevice(screen.snap, fail_restore=True)
    explorer = AppExplorer(device, lambda *_args, **_kw: None)
    explorer.screens = [screen]
    events = []
    checks = ActiveChecks(device, explorer, lambda *args, **_kw: events.append(args), "Details")
    checks.inputs()

    assert [test.status for test in checks.tests] == ["blocked", "blocked"]
    assert checks.tests[0].cleanup.startswith("Original value could not be verified")
    assert "Further input checks stopped" in checks.tests[1].observed
    assert len(events) == 1 and all(name == "Search" for name, _ in device.calls)
