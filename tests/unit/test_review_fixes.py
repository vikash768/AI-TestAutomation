"""Regression tests for the reviewed coverage / false-positive fixes."""
from qaplatform.execute.forms import is_query_form
from qaplatform.execute.reveal import shape_key
from qaplatform.execute.smoke import ComponentRecord, load_url
from qaplatform.explore.crawler import ObservedElement
from qaplatform.ingest.js_parser import CodeElement
from qaplatform.reconcile.matcher import _score

NAV = {"selector": "nav > button", "role": "button", "name": "Test Results", "nav": True}


def _code(**kw):
    base = dict(key="k", file="src/components/Layout.jsx", line=65, kind="button", tag="button", route="(layout)", label="{initials}", handler="inline")
    base.update(kw)
    return CodeElement(**base)


def test_avatar_template_button_never_matches_a_submit_button():
    avatar = _code(attrs={"type": "button", "aria-haspopup": "menu"})
    search = ObservedElement(selector="form > button", role="button", name="Search", tag="button", type="submit")
    assert _score(avatar, search, "https://x.test")[0] == 0


def test_haspopup_expression_form_is_respected():
    menu_btn = _code(attrs={"aria-haspopup": "{true}"})
    plain = ObservedElement(selector="b", role="button", name="Save", tag="button")
    assert _score(menu_btn, plain, "https://x.test")[0] == 0
    opener = ObservedElement(selector="b", role="button", name="JD", tag="button", haspopup="true")
    assert _score(menu_btn, opener, "https://x.test")[0] > 0


def test_query_forms_are_recognised_and_save_forms_are_not():
    assert is_query_form({"submit": {"name": "Search"}, "fields": [{"label": "Patient ID", "type": "text"}]})
    assert is_query_form({"submit": {"name": "Go"}, "fields": [{"label": "", "type": "search"}]})
    assert not is_query_form({"submit": {"name": "Add operator"}, "fields": [{"label": "Email", "type": "email"}, {"label": "Name", "type": "text"}]})
    assert not is_query_form({"submit": {"name": "Save changes"}, "fields": [{"label": "New password", "type": "password"}]})


def test_only_record_ids_are_folded():
    def comp(name):
        return ComponentRecord(key=name, route="#R", kind="button", label=name, name=name, role="button", source="observed", reveal=[NAV])
    assert shape_key(comp("View details for HIV-20261014-190330")) == shape_key(comp("View details for HIV-20261015-144723"))
    assert shape_key(comp("Step 1")) != shape_key(comp("Step 2"))
    assert shape_key(comp("Last 7 days")) != shape_key(comp("Last 30 days"))


def test_tab_screen_loads_without_its_synthetic_fragment():
    assert load_url("https://x.test/#App_Users", [NAV]) == "https://x.test/"
    assert load_url("https://x.test/docs#intro", []) == "https://x.test/docs#intro"  # a real fragment stays


def test_toggle_names_are_the_same_control():
    from qaplatform.execute.smoke import _toggled
    from qaplatform.execute.smoke import _same_name
    assert _toggled("switch to light theme") == "switch to dark theme"

    class Loc:
        def __init__(self, n): self.n = n
        def evaluate(self, js): return self.n
    assert _same_name(Loc("Switch to light theme"), "Switch to dark theme")
    assert not _same_name(Loc("Enable"), "Disable")  # a bare row action never matches its opposite


def test_custom_dropdown_matches_live_combobox_and_inputs_match_by_placeholder():
    dd = _code(kind="clickable", tag="Dropdown", route="#Results", label="Status", attrs={"label": "Status"})
    combo = ObservedElement(selector="b", role="combobox", name="Status", tag="button", type="button", haspopup="listbox")
    assert _score(dd, combo, "https://x.test")[0] >= 5
    ver = _code(kind="input", tag="input", route="#FirmwareReleases", label="0.2.15", attrs={"placeholder": "0.2.15"})
    live = ObservedElement(selector="i", role="textbox", name="Version", tag="input", placeholder="0.2.15")
    assert _score(ver, live, "https://x.test")[0] >= 5


def test_signin_components_take_the_signin_test_outcome():
    from qaplatform.execute.smoke import TestRecord
    from qaplatform.explore.crawler import ObservedPage
    from qaplatform.pipeline import credit_signin_components
    login = ObservedPage(route="/", url="https://x.test/", protected=False, state={"hasPasswordField": True})
    comps = [ComponentRecord(key=k, route="#Login", kind=kind, label=lbl, source="both", page_url="https://x.test/")
             for k, kind, lbl in [("a", "input", "Email"), ("b", "button", "Sign In"), ("c", "checkbox", "Remember me")]]
    tests = [TestRecord(test_key="auth|enter", name="Enter", technique="auth", category="functional", route="/", status="pass"),
             TestRecord(test_key="auth|unknown", name="Unknown", technique="auth", category="functional", route="/", status="pass"),
             TestRecord(test_key="auth|remember", name="Remember", technique="auth", category="functional", route="/", status="fail")]
    credit_signin_components(comps, [login], tests)
    assert [c.status for c in comps] == ["pass", "pass", "fail"]


def test_revealed_copy_of_a_known_screen_is_navigation_not_new_controls():
    from types import SimpleNamespace
    from qaplatform.execute.reveal import Revealer
    known = [ComponentRecord(key=f"k{i}", route="#Test_Results", kind="button", label=n, name=n, role="button", source="observed", selector=f"#b{i}", reveal=[NAV])
             for i, n in enumerate(["Search", "Reset", "Refresh data", "Next"])]
    opener = ComponentRecord(key="op", route="#Dashboard", kind="button", label="Explore test results", source="both", selector="#op")
    rv = Revealer(SimpleNamespace(), lambda *a: None)
    elements = [{"role": "button", "name": n, "selector": f"#x{i}", "visible": True} for i, n in enumerate(["Search", "Reset", "Refresh data", "Next"])]
    fresh = rv.absorb([(opener, "#Dashboard", "https://x.test/", [], elements, None)], known, {}, [], "https://x.test")
    assert fresh == [] and "switches to a screen" in opener.notes


def test_provision_refuses_records_the_app_cannot_delete():
    from types import SimpleNamespace
    from qaplatform.execute.provision import Provisioner
    runner = SimpleNamespace(page=SimpleNamespace(evaluate=lambda js: False))
    pv = Provisioner(runner, lambda *a: None, "t1")
    form = {"submit": {"name": "Create operator"}, "fields": [{"placeholder": "jane.doe"}]}
    pv.components = [ComponentRecord(key="s", route="#AppUsers", kind="button", label="Create operator", name="Create operator", source="both", code_file="src/screens/AppUsers.jsx"),
                     ComponentRecord(key="d", route="#AppUsers", kind="button", label="Disable", source="both", code_file="src/screens/AppUsers.jsx")]
    assert pv._deletable(form)[0] is False
    pv.components.append(ComponentRecord(key="x", route="#AppUsers", kind="button", label="Delete", source="code", code_file="src/screens/AppUsers.jsx"))
    assert pv._deletable(form)[0] is True


def test_template_labelled_avatar_matches_by_structure_only():
    avatar = _code(attrs={"type": "button", "aria-haspopup": "menu"})
    live_avatar = ObservedElement(selector="header button", role="button", name="J", tag="button", type="button", haspopup="menu")
    assert _score(avatar, live_avatar, "https://x.test")[0] >= 5
    plain = _code(label="Refresh", attrs={"type": "button"})
    other = ObservedElement(selector="b", role="button", name="Refresh data now please", tag="button", type="button")
    assert _score(plain, other, "https://x.test")[0] < 5  # no structural bonus for literal labels


def test_form_fields_are_credited_only_when_a_valid_submit_went_through():
    from qaplatform.execute.forms import FormState, FormTester
    from qaplatform.execute.smoke import TestRecord
    st = FormState("#Firmware", "https://x.test/#Firmware", [NAV], True)
    form = {"fields": [{"label": "Version", "placeholder": "0.2.15"}], "submit": {"name": "Upload"}}

    def comps():
        return [ComponentRecord(key="v", route="#FirmwareReleases", kind="input", label="0.2.15", name="Version", source="both",
                                page_url=st.page_url, reveal=[NAV], status="observed"),
                ComponentRecord(key="u", route="#FirmwareReleases", kind="button", label="Upload", name="Upload", source="both",
                                page_url=st.page_url, reveal=[NAV], status="observed")]
    blocked = [TestRecord(test_key="form-wiring|#Firmware|Upload", name="w", technique="form-wiring", category="functional", route="#Firmware", status="blocked"),
               TestRecord(test_key="form-validation|#Firmware|Upload|Version|spaces", name="v", technique="validation", category="validation", route="#Firmware", status="pass")]
    cs = comps()
    FormTester._mark_components(cs, st, form, blocked)
    assert [c.status for c in cs] == ["observed", "observed"]  # nothing was really submitted: no credit

    wired = [TestRecord(test_key="form-wiring|#Firmware|Upload", name="w", technique="form-wiring", category="functional", route="#Firmware", status="pass"),
             TestRecord(test_key="form-failure|#Firmware|Upload", name="f", technique="failure-feedback", category="ux", route="#Firmware", status="fail"),
             TestRecord(test_key="form-validation|#Firmware|Upload|Version|spaces", name="v", technique="validation", category="validation", route="#Firmware", status="pass")]
    cs = comps()
    FormTester._mark_components(cs, st, form, wired)
    assert [c.status for c in cs] == ["pass", "fail"]  # field by its own test; submit carries the feedback failure


def test_user_admin_screen_with_password_fields_is_not_the_sign_in_screen():
    from qaplatform.explore.crawler import ObservedPage
    from qaplatform.reconcile.matcher import reconcile
    users = [_code(key="u1", route="#AppUsers", file="src/screens/AppUsers.jsx", kind="input", tag="input", label="At least 8 characters", attrs={"type": "password", "placeholder": "At least 8 characters"}),
             _code(key="u2", route="#AppUsers", file="src/screens/AppUsers.jsx", label="Reset password", attrs={"type": "button"})]
    login = [_code(key="l1", route="#Login", file="src/screens/Login.jsx", kind="input", tag="input", label="Password", attrs={"type": "password"}),
             _code(key="l2", route="#Login", file="src/screens/Login.jsx", label="Sign In", attrs={"type": "submit"})]
    signed_in = ObservedPage(route="#App_Users", url="https://x.test/#App_Users", protected=True,
                             elements=[ObservedElement(selector="tr button", role="button", name="Reset password", tag="button", type="button")])
    public = ObservedPage(route="/", url="https://x.test/", protected=False,
                          elements=[ObservedElement(selector="form button", role="button", name="Sign In", tag="button", type="submit")])
    res = reconcile(users + login, [signed_in, public], [], "https://x.test")
    page_of = {m.code.key: (m.page.route if m.page else None) for m in res.matches}
    assert page_of["u2"] == "#App_Users" and page_of["l2"] == "/"


def test_screen_reader_only_text_is_not_reported_as_clipped():
    import pytest
    from playwright.sync_api import sync_playwright

    from qaplatform.explore.scripts import LAYOUT_CHECK_JS
    html = """<!doctype html><html><head><style>
      .visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}
      .cut{display:block;width:40px;overflow:hidden;white-space:nowrap}</style></head><body>
      <span class="visually-hidden">App</span><span class="cut">This label really is cut off</span></body></html>"""
    pw = sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = b.new_page()
        page.set_content(html)
        clipped = page.evaluate(LAYOUT_CHECK_JS, {"checkTargets": False})["clipped"]
        assert len(clipped) == 1 and "really is cut off" in str(clipped[0])
        b.close()
    finally:
        pw.stop()
