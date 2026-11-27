from dataclasses import replace

import pytest

from qaplatform.app.checks import tap_results
from qaplatform.app.device import Snapshot
from qaplatform.app.explore import AppExplorer
from qaplatform.explore.crawler import ObservedElement


def control(name, *, tag="Button", role="button", path="", **attrs):
    return ObservedElement(selector=f"title:{name}", name=name, tag=tag, role=role, path_hint=path, **attrs)


class DesktopFixture:
    platform = "macos"

    def __init__(self, states, transitions, *, menus=(), scroll=False, scroll_areas=()):
        self.states, self.transitions = states, transitions
        self.screen = "Home"
        self.actions = []
        self.menus = dict(menus)
        self.menu = ""
        self.has_scroll = scroll
        self.scroll_areas = list(scroll_areas)

    def snapshot(self, settle=0):
        highlights = self.states[self.screen]
        menu_items = self.menus.get(self.menu, []) if self.menu else []
        bars = [control(name, tag="MenuBarItem", role="menuitem", path=trail) for name, trail in self.menus.get("", [])]
        return Snapshot(activity="Fixture", texts=[self.screen], elements=[*highlights, *bars, *menu_items],
                        scroll_areas=self.scroll_areas)

    def screenshot(self, tag):
        return f"{tag}.png"

    def app_state(self):
        return 4

    def tap(self, el):
        self.actions.append((self.screen, el.name))
        if el.tag == "MenuBarItem" or el.tag == "PopUpButton" and el.name in self.menus:
            self.menu = el.name
        else:
            target = self.transitions.get((self.screen, el.name), self.screen)
            self.screen, self.menu = target, ""
        return True

    def hover_menu(self, el):
        self.actions.append((self.menu, el.name))
        self.menu = f"{self.menu}/{el.name}"
        return True

    def scroll_area(self, el, amount):
        self.actions.append((self.screen, "Scroll"))
        if self.has_scroll:
            self.screen = "Scrolled content" if amount > 0 else "Home"
        return True

    def back(self):
        self.menu = ""

    def errors_since_last(self):
        return []

    def toast(self):
        return ""

    def undo(self):
        pass


def walk(dev, **kwargs):
    explorer = AppExplorer(dev, lambda *a, **k: None, **kwargs)
    explorer.run(dev.snapshot())
    return explorer


def test_macos_walks_deep_content_only_panes_and_returns_for_siblings():
    nxt, back, sibling = control("Next section"), control("Back"), control("Explore overview")
    titles = ["Home", *[f"Section {number}" for number in range(1, 6)]]
    states = {name: [nxt, back] for name in titles}
    states["Home"] = [nxt, sibling]
    states["Overview"] = [back]
    transitions = {(name, nxt.name): titles[index + 1] for index, name in enumerate(titles[:-1])}
    transitions.update({(name, back.name): titles[index - 1] for index, name in enumerate(titles) if index})
    transitions.update({("Home", sibling.name): "Overview", ("Overview", back.name): "Home"})
    fixture = DesktopFixture(states, transitions)

    result = walk(fixture, max_depth=12, max_screens=20)
    seen = {screen.snap.texts[0] for screen in result.screens}
    assert seen == {*titles, "Overview"}
    assert max(screen.depth for screen in result.screens) >= 5
    assert ("Home", "Explore overview") in fixture.actions
    assert result.coverage_details()["complete_within_scope"]


def test_macos_finds_submenus_and_walks_their_window_destination():
    menu_path = "/Application[1]/MenuBar[1]/MenuBarItem[2]"
    layout = control("Layout", tag="MenuItem", role="menuitem", path=f"{menu_path}/Menu[1]/MenuItem[1]", haspopup="menu")
    details = control("Show inspector", tag="MenuItem", role="menuitem", path=f"{layout.path_hint}/Menu[1]/MenuItem[1]")
    danger = control("Delete record", tag="MenuItem", role="menuitem", path=f"{menu_path}/Menu[1]/MenuItem[2]")
    settings = control("Settings…", tag="MenuItem", role="menuitem", path=f"{menu_path}/Menu[1]/MenuItem[3]")
    external = control("Settings in System Settings", tag="MenuItem", role="menuitem", path=f"{menu_path}/Menu[1]/MenuItem[4]")
    inspector = control("Inspector contents")
    dismiss = control("Cancel", in_dialog=True)
    fixture = DesktopFixture(
        {"Home": [control("Dashboard")], "Inspector": [inspector, dismiss], "Settings": [control("Back")]},
        {("Home", details.name): "Inspector", ("Inspector", dismiss.name): "Home", ("Home", settings.name): "Settings",
         ("Settings", "Back"): "Home"},
        menus=[("", [("Apple", "/Application[1]/MenuBar[1]/MenuBarItem[1]"), ("View", menu_path)]),
               ("View", [layout, danger, settings, external]), ("View/Layout", [layout, danger, settings, external, details])],
    )

    result = walk(fixture, max_depth=12, max_screens=20)
    assert {screen.route for screen in result.screens if screen.kind == "menu"} == {"menu:View", "menu:View > Layout"}
    assert ("Inspector", inspector.name) in fixture.actions
    assert ("Settings", "Back") in fixture.actions
    assert not any(name in ("Apple", danger.name, external.name) for _, name in fixture.actions)
    held = [tap for tap in result.taps if tap.element.name == danger.name]
    assert len(held) == 1 and held[0].action_class == "destructive"


def test_macos_records_controls_left_when_navigation_cannot_be_restored():
    enters, sibling = control("Explore one-way panel"), control("Explore the other section")
    fixture = DesktopFixture({"Home": [enters, sibling], "One-way panel": [control("Panel details")]},
                             {("Home", enters.name): "One-way panel"})
    result = walk(fixture, max_depth=12)

    summary = result.coverage_details()
    assert ("One-way panel", "Panel details") in fixture.actions
    assert summary["unreachable_items"] == [{
        "screen": "Home", "label": sibling.name, "reason": "could not safely return to this window or section",
    }]
    assert not summary["complete_within_scope"]


def test_macos_scrolls_verified_regions_to_reveal_controls_and_restores_viewport():
    area = control("Results", tag="ScrollView", role="scrollarea", bbox=[0, 0, 720, 500])
    fixture = DesktopFixture({"Home": [control("First result")], "Scrolled content": [control("Last result")]},
                             {}, scroll=True, scroll_areas=[area])
    result = walk(fixture, max_depth=12)

    assert ("Scrolled content", "Last result") in fixture.actions
    assert any(screen.snap.texts == ["Scrolled content"] for screen in result.screens)
    assert fixture.screen == "Home"
    assert result.coverage_details()["complete_within_scope"]


def test_macos_inventory_of_dropdown_choices_preserves_other_controls_and_permissions():
    popup_path = "/Application[1]/Window[1]/PopUpButton[1]"
    dropdown = control("Projects", tag="PopUpButton", role="combobox", path=popup_path)
    option = control("Project A", tag="MenuItem", role="menuitem", path=f"{popup_path}/Menu[1]/MenuItem[1]")
    settings = control("Settings")
    grant = control("Grant permissions")
    fixture = DesktopFixture({"Home": [dropdown, settings, grant], "Settings": [control("Back")]},
                             {("Home", settings.name): "Settings", ("Settings", "Back"): "Home"},
                             menus=[(dropdown.name, [option])])
    result = walk(fixture)

    assert (("Home", dropdown.name) in fixture.actions and ("Home", settings.name) in fixture.actions)
    assert not {grant.name, option.name} & {name for _, name in fixture.actions}
    assert fixture.menu == ""
    assert any(screen.kind == "menu" and screen.snap.elements == [option] for screen in result.screens)
    assert {tap.element.name for tap in result.taps if tap.result == "skipped"} == {grant.name, option.name}
    assert result.coverage_details()["control_types"]["combobox"] == {"observed": 1, "exercised": 1, "held": 0, "unreachable": 0}


class ToggleFixture(DesktopFixture):
    def __init__(self, toggle, behavior):
        super().__init__({"Home": [toggle, control("Open section")]}, {})
        self.original = toggle
        self.behavior = behavior

    def tap(self, el):
        if el.name == self.original.name and (self.behavior == "normal" or self.behavior == "restore fails" and not el.checked):
            self.states["Home"][0] = replace(el, checked=not el.checked)
        return super().tap(el)


@pytest.mark.parametrize(("role", "tag"), [("checkbox", "CheckBox"), ("switch", "Switch")])
@pytest.mark.parametrize(("behavior", "status"), [("normal", "pass"), ("broken", "fail"), ("restore fails", "blocked")])
def test_macos_verifies_toggle_response_and_restoration(role, tag, behavior, status):
    toggle = control("Show details", role=role, tag=tag, checked=False)
    fixture = ToggleFixture(toggle, behavior)
    result = walk(fixture)
    tests, findings = tap_results(result, {})

    assert tests[0].technique == "state-change"
    assert tests[0].status == status
    assert fixture.states["Home"][0].checked is (behavior == "restore fails")
    assert len([name for _, name in fixture.actions if name == toggle.name]) == (1 if behavior == "broken" else 2)
    if behavior == "normal":
        assert tests[0].observed == "Changed from off to on and restored to off"
    if behavior == "broken":
        assert any("does not toggle" in finding.title for finding in findings)
    if behavior == "restore fails":
        assert not result.coverage_details()["complete_within_scope"]
        assert ("Home", "Open section") not in fixture.actions


def test_macos_reports_no_testable_window_content_and_budget():
    fixture = DesktopFixture({"Home": [control("Close")]}, {})
    fixture.snapshot = lambda settle=0: Snapshot(activity="Fixture", elements=fixture.states["Home"])
    limited = walk(fixture, budget_seconds=0)

    coverage = limited.coverage_details()
    assert "The app window exposed no testable content through macOS accessibility" in coverage["warnings"]
    assert not fixture.actions and not coverage["complete_within_scope"]

    fixture.states["Home"] = [control("Sections")]
    limited = walk(fixture, budget_seconds=0)
    assert any("time budget" in limit for limit in limited.coverage_details()["limits"])
    assert limited.coverage_details()["unreachable"] == 1
