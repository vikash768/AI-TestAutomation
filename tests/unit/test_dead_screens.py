from pathlib import Path

from qaplatform.ingest.js_parser import build_inventory
from qaplatform.pipeline import dead_screen_checks


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_unreachable_screens_are_excluded_and_reported(tmp_path):
    _write(tmp_path, "src/main.jsx", "import App from './App.jsx';\nexport default App;\n")
    _write(tmp_path, "src/App.jsx", "import Home from './screens/Home.jsx';\nexport default function App(){ return <Home/>; }\n")
    _write(tmp_path, "src/screens/Home.jsx", "export default function Home(){ return <button onClick={() => 1}>Go</button>; }\n")
    _write(tmp_path, "src/screens/Orphan.jsx", "export default function Orphan(){ return <button onClick={() => 2}>Lost</button>; }\n")

    inv = build_inventory(tmp_path, "react")

    assert inv.routeless
    assert "#Home" in inv.page_files and "#Orphan" not in inv.page_files
    assert inv.dead_files == ["src/screens/Orphan.jsx"]
    tests, findings = dead_screen_checks(inv)
    assert [t.status for t in tests] == ["fail"]
    assert findings[0].route == "(codebase)" and "Orphan" in findings[0].title


def test_tab_screen_components_replay_the_nav_click():
    """Router-less tabs live behind a nav click, not a URL; components must carry that click."""
    from qaplatform.explore.crawler import ObservedElement, ObservedPage
    from qaplatform.ingest.js_parser import CodeElement
    from qaplatform.pipeline import build_components
    from qaplatform.reconcile.matcher import reconcile

    nav = {"selector": "nav > button:nth-of-type(2)", "role": "button", "name": "App Users", "nav": True}
    page = ObservedPage(route="#App_Users", url="https://x.test/#App_Users", protected=True, reveal=[nav],
                        elements=[ObservedElement(selector="#add", role="button", name="Add operator", tag="button")])
    code = CodeElement(key="a::btn", file="src/screens/AppUsers.jsx", line=10, kind="button", tag="button", route="#AppUsers", label="Add operator", handler="openAdd")
    comps = build_components(reconcile([code], [page], [], "https://x.test"), {})
    add = next(c for c in comps if c.label == "Add operator")
    assert add.reveal == [nav]


def test_tab_components_are_top_level_but_dialog_contents_are_not():
    from qaplatform.execute.reveal import shape_key
    from qaplatform.execute.smoke import ComponentRecord, layered

    nav = {"selector": "nav > button", "role": "button", "name": "App Users", "nav": True}
    on_tab = ComponentRecord(key="k1", route="#App_Users", kind="button", label="Add operator", source="both", reveal=[nav])
    in_dialog = ComponentRecord(key="k2", route="#App_Users", kind="button", label="Save", source="both", reveal=[nav, {"name": "Add operator"}])
    assert not layered(on_tab) and layered(in_dialog)

    rows = [ComponentRecord(key=f"r{i}", route="#Results", kind="button", label=n, name=n, role="button", source="observed", reveal=[nav])
            for i, n in enumerate(["View details for HIV-20261014-190330", "View details for HIV-20261015-144723"])]
    assert shape_key(rows[0]) == shape_key(rows[1])


def test_lazy_barrel_and_alias_imports_count_as_reachable(tmp_path):
    _write(tmp_path, "package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "src/main.jsx", "\n".join([
        "import { lazy } from 'react';",
        "import { Users } from './screens';",
        "import Home from '@/views/Home';",
        "const Orders = lazy(() => import('./views/Orders'));",
        "const Legacy = require('./views/Legacy');",
        "export default function App(){ return <><Users/><Home/><Orders/><Legacy/></>; }",
    ]))
    _write(tmp_path, "src/screens/index.js", "export { default as Users } from './Users';\n")
    _write(tmp_path, "src/screens/Users.jsx", "export default function Users(){ return <button onClick={() => 1}>Add</button>; }\n")
    _write(tmp_path, "src/views/Home.jsx", "export default function Home(){ return <button onClick={() => 1}>Home</button>; }\n")
    _write(tmp_path, "src/views/Orders.jsx", "export default function Orders(){ return <button onClick={() => 1}>Orders</button>; }\n")
    _write(tmp_path, "src/views/Legacy.jsx", "export default function Legacy(){ return <button onClick={() => 1}>Old</button>; }\n")
    _write(tmp_path, "src/views/Orphan.jsx", "export default function Orphan(){ return <button onClick={() => 1}>Lost</button>; }\n")

    inv = build_inventory(tmp_path, "react")

    assert inv.dead_files == ["src/views/Orphan.jsx"]
    assert {"#Users", "#Home", "#Orders", "#Legacy"} <= set(inv.page_files)
    assert "#index" not in inv.page_files  # a barrel is not a screen


def test_unresolvable_local_import_reports_nothing_dead(tmp_path):
    _write(tmp_path, "src/main.jsx", "import Users from '#screens/Users';\nexport default Users;\n")
    _write(tmp_path, "src/screens/Users.jsx", "export default function Users(){ return <button onClick={() => 1}>Add</button>; }\n")
    _write(tmp_path, "src/screens/Other.jsx", "export default function Other(){ return <button onClick={() => 1}>X</button>; }\n")

    inv = build_inventory(tmp_path, "react")

    assert inv.dead_files == []
    assert {"#Users", "#Other"} <= set(inv.page_files)
    assert any("reachability incomplete" in w for w in inv.warnings)
