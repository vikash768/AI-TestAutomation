import os
from pathlib import Path

import pytest

from qaplatform.ingest.intent import _api_index, build_intent
from qaplatform.ingest.js_parser import build_inventory
from qaplatform.reconcile.matcher import INTERACTIVE_KINDS
from tests.unit.fixtures_app import write_app


@pytest.fixture(scope="module")
def inv(tmp_path_factory):
    root = write_app(tmp_path_factory.mktemp("mini"))
    return build_inventory(root, "react")


def by_label(inv, label, route=None):
    return [e for e in inv.elements if e.label == label and (route is None or e.route == route)]


def test_routes_and_guards(inv):
    paths = {r.path: r for r in inv.routes}
    assert set(paths) == {"/login", "/home", "/settings"}
    assert paths["/home"].protected and paths["/settings"].protected and not paths["/login"].protected
    assert paths["/home"].layouts == ["Shell"]
    assert inv.page_files["/home"].endswith("src/pages/Home.jsx")


def test_wrappers_resolved(inv):
    assert inv.wrappers["Button"] == "button"
    assert inv.wrappers["Field"] == "input"
    assert inv.wrappers["NavLink"] == "link"


def test_nav_array_expands_to_links_without_icon_clones(inv):
    links = [e for e in inv.elements if e.route == "(layout)" and e.kind == "link"]
    assert sorted(l.label for l in links) == ["Home", "Settings"]
    assert all(l.href in ("/home", "/settings") for l in links)
    assert not [e for e in inv.elements if e.route == "(layout)" and e.kind == "clickable" and e.label in ("Home", "Settings")]


def test_logout_is_session_ending(inv):
    (logout,) = by_label(inv, "Log out")
    intent = build_intent(logout, inv, _api_index(inv))
    assert intent["session_ending"] and intent["action_class"] == "session_ending"
    assert "/login" in logout.navigates_to


def test_login_page_labels_and_alternatives(inv):
    (email,) = by_label(inv, "Email", "/login")
    assert email.kind == "input" and email.attrs.get("required") is True and email.attrs.get("type") == "email"
    (toggle,) = by_label(inv, "Hide password", "/login")
    assert "Show password" in toggle.label_alternatives
    (submit,) = by_label(inv, "Sign in", "/login")
    assert submit.kind == "button" and submit.in_form and submit.form_handler == "handleSubmit"
    assert build_intent(submit, inv, _api_index(inv))["action_class"] == "mutating"
    assert [e for e in inv.elements if e.route == "/login" and e.kind == "form"]


def test_handler_facts_and_action_classes(inv):
    api = _api_index(inv)
    (edit,) = by_label(inv, "Edit thing", "/home")
    i = build_intent(edit, inv, api)
    assert "drawer" in edit.opens and i["action_class"] == "safe"
    assert any("updateThing" in d for d in edit.deferred_calls)
    (delete,) = by_label(inv, "Delete thing", "/home")
    i = build_intent(delete, inv, api)
    assert i["action_class"] == "destructive" and i["api_calls"][0]["method"] == "DELETE"
    (react,) = by_label(inv, "Reactivate", "/home")
    assert build_intent(react, inv, api)["action_class"] == "mutating"
    (link,) = [e for e in by_label(inv, "Go to settings", "/home") if e.kind == "link"]
    assert link.href == "/settings" and build_intent(link, inv, api)["navigates_to"] == ["/settings"]
    (help_link,) = by_label(inv, "Help", "/home")
    assert help_link.conditional and help_link.href.startswith("https://")


def test_derived_tabs_and_switch(inv):
    tabs = [e for e in inv.elements if e.route == "/settings" and e.kind == "button"]
    assert sorted(t.label for t in tabs) == ["Danger Zone", "Email", "General"]
    assert all(t.conditional for t in tabs)
    (switch,) = [e for e in inv.elements if e.route == "/settings" and e.kind == "switch"]
    assert switch.label == "Enable thing"


def test_modal_children_are_conditional(inv):
    close = [e for e in inv.elements if e.label == "Close"]
    assert close and all(e.conditional for e in close)


def test_api_timers_and_facts(inv):
    methods = {(a.function, a.method) for a in inv.api_calls}
    assert ("deleteThing", "DELETE") in methods and ("poll", "GET") in methods
    assert any(t.kind == "setInterval" and t.ms == 15000 for t in inv.timers)
    assert any(n.target == "/login" and n.component == "ProtectedRoute" for n in inv.navigates)


def test_no_display_components_leak(inv):
    assert all(e.kind in INTERACTIVE_KINDS or e.kind == "form" for e in inv.elements)


TRACKPILOT = os.environ.get("TRACKPILOT_FE_PATH", "")


@pytest.mark.skipif(not TRACKPILOT or not Path(TRACKPILOT).exists(), reason="TRACKPILOT_FE_PATH not set")
def test_trackpilot_repo_parses():
    inv = build_inventory(Path(TRACKPILOT), "react")
    assert len(inv.routes) >= 19
    assert inv.wrappers.get("DropdownMenu") == "button"
    labels = {e.label for e in inv.elements}
    assert {"Log out", "Sign in", "+ Add User", "Actions ▾", "Danger Zone"} <= labels
    assert len([e for e in inv.elements if e.kind in INTERACTIVE_KINDS]) > 250


def test_drawer_content_is_conditional(tmp_path):
    root = tmp_path / "d"
    (root / "src/pages").mkdir(parents=True)
    (root / "package.json").write_text('{"dependencies":{"react":"18","react-router-dom":"6"}}')
    (root / "src/App.jsx").write_text("import { Route, Routes } from 'react-router-dom';\nimport P from './pages/P';\nexport default function App(){ return (<Routes><Route path=\"/p\" element={<P />} /></Routes>); }\n")
    (root / "src/pages/P.jsx").write_text("""
function EditForm({ onSave }) { return (<form onSubmit={onSave}><input id=\"n\" onChange={() => 1} /><label>Agree <input type=\"checkbox\" onChange={() => 1} /></label><button type=\"submit\">Save thing</button></form>); }
export default function P() {
  function openEdit() { openDrawer({ title: 'Edit', content: <EditForm onSave={() => 1} /> }); }
  return (<div><button onClick={openEdit}>Edit</button><button onClick={() => x()}>Always here</button></div>);
}
""")
    inv = build_inventory(root, "react")
    by = {e.label: e for e in inv.elements if e.route == "/p"}
    assert by["Save thing"].conditional and by["Agree"].conditional and by["Agree"].kind == "checkbox"
    assert not by["Always here"].conditional and by["Edit"].opens == ["drawer"]


def test_early_returns_callbacks_and_wrapper_children(tmp_path):
    root = tmp_path / "e"
    (root / "src/pages").mkdir(parents=True)
    (root / "package.json").write_text('{"dependencies":{"react":"18","react-router-dom":"6"}}')
    (root / "src/App.jsx").write_text("import { Route, Routes } from 'react-router-dom';\nimport P from './pages/P';\nexport default function App(){ return (<Routes><Route path=\"/p\" element={<P />} /></Routes>); }\n")
    (root / "src/pages/P.jsx").write_text("""
const DAYS = [['mon', 'Mon'], ['tue', 'Tue']];
function Menu({ label = 'Actions', children }) { const [open, setOpen] = useState(false); return (<><button onClick={() => setOpen(!open)}>{label}</button>{open && <div role=\"menu\">{children}</div>}</>); }
function Item({ onClick, children }) { return <button role=\"menuitem\" onClick={onClick}>{children}</button>; }
function Tile({ label, href }) { return <a href={href}>Download for {label}</a>; }
function Picker({ label = 'Date range' }) { return <button onClick={() => 1}>{`${label}: ${fmt()}`}</button>; }
export default function P() {
  const [tab, setTab] = useState('a');
  if (!org) { return (<div><button onClick={() => create()}>Create org</button></div>); }
  return (<div>
    <Tile label=\"Mac\" href=\"/m\" /><Tile label=\"Windows\" href=\"/w\" />
    <Picker />
    <Menu><Item onClick={() => edit()}>Edit role</Item></Menu>
    {tab === 'hours' && (<div>{DAYS.map(([k, l]) => <button key={k} onClick={() => toggle(k)}>{l}</button>)}</div>)}
    <button onClick={() => setTab('hours')}>Hours</button>
  </div>);
}
""")
    inv = build_inventory(root, "react")
    els = {e.label: e for e in inv.elements if e.route == "/p"}
    assert els["Create org"].conditional                      # early return
    assert els["Mon"].conditional and els["Tue"].conditional   # inside {cond && ...} through a .map callback
    assert not els["Hours"].conditional
    assert els["Edit role"].conditional and els["Edit role"].kind == "menuitem"  # Menu renders children only when open
    assert els["Download for Mac"].kind == "link" and els["Download for Windows"].kind == "link"  # root label template + prop
    assert any(l.startswith("Date range: ") for l in els)       # default param substituted into the template
    assert not els["Actions"].conditional


def test_inline_array_map_and_repeated_components(tmp_path):
    root = tmp_path / "r"
    (root / "src/pages").mkdir(parents=True)
    (root / "package.json").write_text('{"dependencies":{"react":"18","react-router-dom":"6"}}')
    (root / "src/App.jsx").write_text("import { Route, Routes } from 'react-router-dom';\nimport P from './pages/P';\nexport default function App(){ return (<Routes><Route path=\"/p\" element={<P />} /></Routes>); }\n")
    (root / "src/pages/P.jsx").write_text("""
import { Link } from 'react-router-dom';
const REPORTS = [{ to: '/r/a', title: 'A', icon: IconA }, { to: '/r/b', title: 'B', icon: IconB }];
function RowMenu({ row }) { return <button onClick={() => act(row)}>Row actions</button>; }
export default function P({ rows, n }) {
  return (<div>
    {[['one', 'One'], ['two', 'Two'], ...(n > 0 ? [['del', `Deleted (${n})`]] : [])].map(([key, label]) => <button key={key} onClick={() => setTab(key)}>{label}</button>)}
    {REPORTS.map((r) => { const Icon = r.icon; return (<div key={r.to}><Icon /><Link to={r.to}><Button>Open</Button></Link></div>); })}
    {rows.map((row) => <RowMenu key={row.id} row={row} />)}
  </div>);
}
export function Button(props) { return <button {...props} />; }
""")
    inv = build_inventory(root, "react")
    els = [e for e in inv.elements if e.route == "/p"]
    labels = sorted(str(e.label) for e in els)
    assert "One" in labels and "Two" in labels and "Deleted (${n})" in labels
    links = [e for e in els if e.kind == "link"]
    assert sorted(l.href for l in links) == ["/r/a", "/r/b"] and all(l.label == "Open" for l in links)
    buttons = [e for e in els if e.kind == "button" and e.label == "Open"]
    assert len(buttons) == 2 and all(b.href is None for b in buttons)
    assert len({e.key for e in els}) == len(els)  # keys are unique even for identical clones
    (menu,) = [e for e in els if e.label == "Row actions"]
    assert menu.repeated
    assert not [e for e in els if e.tag in ("Icon", "IconA")]
