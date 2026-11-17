"""Static extraction of UI intent from a native app codebase.

Two shapes of codebase are understood, both through the same tree-sitter
grammars as the web ingest:

  React Native   screens from React Navigation (<Stack.Screen name component>)
                 or Expo Router (app/ files); every Touchable / Pressable /
                 Button / TextInput / Switch, or any component given onPress,
                 with its label, handler, navigation target and API calls
  Electron       menu templates ({label, click | role | accelerator | submenu})
                 grouped by the menu they sit in

The result is a CodeInventory (routes = screens or menus) so the lead's
reconcile stage and report work unchanged. Nothing here talks to an LLM.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tree_sitter import Node, Parser

from qaplatform.ingest.js_parser import (
    SOURCE_SUFFIXES, ApiCall, CodeElement, CodeInventory, CodeRoute, StorageFact, TimerFact,
    _attr_map, _lang_for, _plain, _resolve_import, _string_value, _tag_name, _text, _walk,
)

RN_KIND = {
    "Button": "button", "TouchableOpacity": "button", "TouchableHighlight": "button", "TouchableWithoutFeedback": "button",
    "TouchableNativeFeedback": "button", "Pressable": "button", "IconButton": "button", "FAB": "button",
    "TextInput": "input", "Switch": "switch", "CheckBox": "checkbox", "Checkbox": "checkbox", "RadioButton": "radio",
}
SKIP_DIRS = {"node_modules", "android", "ios", "build", "dist", ".git", "coverage", "__tests__", "__mocks__", ".expo", "vendor", "Pods", "drawio"}
AUTH_SCREEN_RE = re.compile(r"(login|signin|sign_in|signup|register|auth|otp|forgot|reset|onboard|splash|welcome|landing|verify)", re.I)
NAV_RE = re.compile(r"\b(?:navigation|navigate|router|nav|props\.navigation)\s*\.?\s*(?:navigate|push|replace|reset|jumpTo|popTo)?\s*\(\s*\{?\s*(?:name\s*:\s*)?['\"`]([^'\"`$]+)['\"`]"
                    r"|\breset\s*\(\s*\{[^}]*?routes\s*:\s*\[\s*\{\s*name\s*:\s*['\"`]([^'\"`$]+)['\"`]")
WRITE_RE = re.compile(r"\.(post|put|patch|delete)\s*\(|method\s*:\s*['\"](POST|PUT|PATCH|DELETE)['\"]", re.I)
API_RE = re.compile(r"\b(?:axios|api|http|client|request|instance)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*['\"`]([^'\"`]+)", re.I)
FETCH_RE = re.compile(r"\bfetch\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*(?:,\s*\{[^}]*method\s*:\s*['\"](\w+))?", re.I)
LOGOUT_RE = re.compile(r"\b(log ?out|sign ?out|logout|signOut|clearSession)\b", re.I)
ALERT_RE = re.compile(r"Alert\.alert\s*\(")
TOAST_RE = re.compile(r"\b(toast|Toast|ToastAndroid|Snackbar|showMessage)\s*\.?\s*(show\w*|success|error|info)?\s*\(")
ITEM_LABEL_KEYS = ("title", "label", "name", "text", "accessibilityLabel")
ITEM_HANDLER_KEYS = ("onPress", "action", "onSelect", "handler", "onClick")
ITEM_ROUTE_KEYS = ("route", "screen", "to", "target", "key")
STORAGE_RE = re.compile(r"(AsyncStorage|SecureStore|MMKV|storage)\s*\.\s*(setItem|getItem|removeItem|set|getString|delete)\w*\s*\(\s*['\"`]([^'\"`]+)")
TIMER_RE = re.compile(r"\b(setInterval|setTimeout)\s*\([^;]*?,\s*(\d+)\s*\)")
ELECTRON_ROLE_LABEL = {
    "undo": "Undo", "redo": "Redo", "cut": "Cut", "copy": "Copy", "paste": "Paste", "pasteAndMatchStyle": "Paste and Match Style",
    "selectAll": "Select All", "delete": "Delete", "minimize": "Minimize", "close": "Close Window", "zoom": "Zoom", "quit": "Quit {app}",
    "hide": "Hide {app}", "hideothers": "Hide Others", "hideOthers": "Hide Others", "unhide": "Show All", "about": "About {app}",
    "togglefullscreen": "Toggle Full Screen", "toggleFullScreen": "Toggle Full Screen", "reload": "Reload", "forceReload": "Force Reload",
    "toggleDevTools": "Toggle Developer Tools", "resetZoom": "Actual Size", "zoomIn": "Zoom In", "zoomOut": "Zoom Out", "front": "Bring All to Front",
    "window": "Window", "help": "Help", "services": "Services", "appMenu": "{app}", "fileMenu": "File", "editMenu": "Edit", "viewMenu": "View", "windowMenu": "Window",
}


def _files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in rel) or ".test." in path.name or ".spec." in path.name or path.name.endswith(".d.ts"):
            continue
        if path.name in ("babel.config.js", "metro.config.js", "jest.config.js", "app.config.js", "eslint.config.js", ".eslintrc.js", "tailwind.config.js"):
            continue
        out.append(path)
    return out


def _parse(path: Path) -> Node:
    return Parser(_lang_for(path)).parse(path.read_bytes()).root_node


def _function_bodies(root: Node) -> dict[str, str]:
    """name -> source of every named function in a file (const x = () => {}, function x() {}, x() {})."""
    out: dict[str, str] = {}
    for n in _walk(root):
        if n.type == "function_declaration" and n.child_by_field_name("name") is not None:
            out[_text(n.child_by_field_name("name"))] = _text(n)
        elif n.type == "variable_declarator":
            val = n.child_by_field_name("value")
            if val is not None and val.type in ("arrow_function", "function_expression", "function", "call_expression"):
                out[_text(n.child_by_field_name("name"))] = _text(val)
        elif n.type == "method_definition" and n.child_by_field_name("name") is not None:
            out[_text(n.child_by_field_name("name"))] = _text(n)
    return out


def _imports(root: Node, path: Path, repo: Path) -> dict[str, str]:
    """local name -> repo-relative file for relative imports."""
    out: dict[str, str] = {}
    for n in root.children:
        if n.type != "import_statement":
            continue
        src = n.child_by_field_name("source")
        spec = _string_value(src) if src is not None else None
        if not spec or not (spec.startswith(".") or spec.startswith("@/") or spec.startswith("~/") or spec.startswith("src/")):
            continue
        spec = re.sub(r"^[@~]/", "src/" if (repo / "src").exists() else "", spec)
        target = _resolve_import(path, spec, repo) if spec.startswith(".") else _resolve_import(repo / "x.js", "./" + spec, repo)
        if not target:
            continue
        for c in _walk(n):
            if c.type == "identifier":
                out[_text(c)] = target
    return out


def _children_text(el: Node) -> list[str]:
    """Literal text a JSX element renders: <Text>Save</Text>, {'Save'}, {t('save')}."""
    out = []
    for d in _walk(el):
        if d.type == "jsx_text":
            t = _text(d).strip()
            if t:
                out.append(t)
        elif d.type == "jsx_expression":
            for c in d.children:
                v = _string_value(c) if c.type in ("string", "template_string") else None
                if v and v.strip():
                    out.append(v.strip())
    return out


def _label(tag: str, attrs: dict, el: Node) -> tuple[str | None, list[str]]:
    for key in ("accessibilityLabel", "aria-label", "title", "label", "placeholder", "accessibilityHint"):
        kind_val = attrs.get(key)
        if kind_val and kind_val[0] in ("str", "template") and kind_val[1]:
            return kind_val[1], []
        if kind_val and kind_val[0] == "alts" and kind_val[1]:
            return kind_val[1][0], kind_val[1][1:]
    texts = _children_text(el)
    return (texts[0], texts[1:3]) if texts else (None, [])


def _handler_source(attrs: dict, bodies: dict[str, str], key: str) -> tuple[str, str]:
    kind_val = attrs.get(key)
    if not kind_val:
        return "", ""
    kind, val = kind_val
    if kind == "inline":
        src = val
        name = "inline"
    elif kind in ("ident", "member"):
        name = str(val).split(".")[-1]
        src = bodies.get(name, "")
    else:
        name, src = str(val)[:60], str(val)
    # one hop: handler body calling another local function
    for called in re.findall(r"\b([a-zA-Z_]\w*)\s*\(", src):
        if called in bodies and called != name and len(src) < 4000:
            src += "\n" + bodies[called][:2000]
    return name, src


def _screen_routes(root: Node, rel: str, imports: dict[str, str]) -> list[tuple[CodeRoute, str | None]]:
    """<X.Screen name="Home" component={HomeScreen} options={{title: 'Home'}} />"""
    out = []
    for n in _walk(root):
        if n.type not in ("jsx_self_closing_element", "jsx_opening_element"):
            continue
        tag = _tag_name(n)
        if not (tag.endswith(".Screen") or tag == "Screen"):
            continue
        attrs = _attr_map(n)
        name = attrs.get("name")
        if not name or name[0] != "str":
            continue
        comp = attrs.get("component")
        comp_name = comp[1] if comp and comp[0] in ("ident", "member") else None
        if comp_name is None and n.type == "jsx_opening_element" and n.parent is not None:
            inner = [_tag_name(c) for c in _walk(n.parent) if c.type in ("jsx_self_closing_element", "jsx_opening_element") and c is not n]
            comp_name = next((t for t in inner if t and t[0].isupper()), None)
        route = CodeRoute(path=name[1], element=comp_name, protected=not AUTH_SCREEN_RE.search(name[1]), file=rel, line=n.start_point[0] + 1)
        out.append((route, imports.get(comp_name or "")))
    return out


def _expo_route(rel: str) -> str | None:
    parts = Path(rel).with_suffix("").parts
    if not parts or parts[0] != "app" or Path(rel).name.startswith("_"):
        return None
    segs = [p for p in parts[1:] if not (p.startswith("(") and p.endswith(")"))]
    if segs and segs[-1] == "index":
        segs = segs[:-1]
    return "/" + "/".join(segs)


def _electron_menus(root: Node, rel: str, app_name: str) -> list[CodeElement]:
    """Menu items from Electron templates, routed to 'menu:<parent label>'."""

    def prop(obj: Node, key: str) -> Node | None:
        for p in obj.children:
            if p.type == "pair" and _text(p.child_by_field_name("key")).strip("'\"") == key:
                return p.child_by_field_name("value")
            if p.type == "method_definition" and _text(p.child_by_field_name("name")) == key:
                return p
        return None

    def str_of(v: Node | None) -> str | None:
        if v is None:
            return None
        if v.type in ("string", "template_string"):
            return _string_value(v)
        if v.type == "member_expression" and _text(v) in ("app.name", "app.getName()"):
            return app_name
        if v.type == "binary_expression":
            parts = [str_of(c) for c in v.children if c.type != "+"]
            return "".join(p for p in parts if p is not None) or None
        if v.type == "call_expression" and _text(v.child_by_field_name("function")) in ("app.getName",):
            return app_name
        return None

    def menu_label(obj: Node) -> str | None:
        return str_of(prop(obj, "label")) or ELECTRON_ROLE_LABEL.get(str_of(prop(obj, "role")) or "", "").replace("{app}", app_name) or None

    # items declared as variables and placed into a submenu later: `submenu: [zoomIn, ...(x ? [check] : [])]`
    var_parent: dict[str, str] = {}
    for obj in _walk(root):
        if obj.type == "object" and (sub := prop(obj, "submenu")) is not None and sub.type == "array" and (parent := menu_label(obj)):
            for d in _walk(sub):
                if d.type == "identifier" and d.parent is not None and d.parent.type in ("array", "spread_element", "ternary_expression"):
                    var_parent.setdefault(_text(d), parent)

    out: list[CodeElement] = []
    for obj in _walk(root):
        if obj.type != "object":
            continue
        label_node, role_node = prop(obj, "label"), prop(obj, "role")
        if prop(obj, "click") is None and role_node is None and prop(obj, "accelerator") is None and prop(obj, "submenu") is None:
            continue
        label = str_of(label_node)
        role = str_of(role_node) or ""
        if label is None and role:
            label = ELECTRON_ROLE_LABEL.get(role, role).replace("{app}", app_name)
        if not label or str_of(prop(obj, "type")) == "separator":
            continue
        parent_label = None
        cur = obj.parent
        while cur is not None:
            if cur.type == "object" and cur is not obj and prop(cur, "submenu") is not None:
                parent_label = menu_label(cur)
                break
            if cur.type == "variable_declarator":
                parent_label = var_parent.get(_text(cur.child_by_field_name("name")))
                break
            cur = cur.parent
        click = prop(obj, "click")
        handler_src = _text(click) if click is not None else ""
        el = CodeElement(file=rel, line=obj.start_point[0] + 1, tag="MenuItem", kind="menuitem", label=label, handler=(role or ("click" if click is not None else None)),
                         event="click", attrs={"role": role, "accelerator": str_of(prop(obj, "accelerator")) or "", "submenu": prop(obj, "submenu") is not None,
                                              "type": str_of(prop(obj, "type")) or "", "checked": _text(prop(obj, "checked")) if prop(obj, "checked") is not None else None},
                         route=f"menu:{parent_label}" if parent_label else "menubar", calls=re.findall(r"\b([a-zA-Z_]\w*)\s*\(", handler_src)[:20])
        if (prop(obj, "enabled") is not None and _text(prop(obj, "enabled")) == "false") or prop(obj, "visible") is not None:
            el.conditional = True
        out.append(el)
    return out


def build_app_inventory(repo_root: Path, platform: str) -> tuple[CodeInventory, dict[str, set[str]]]:
    """Return the inventory and, per screen, the literal texts its files render (for screen matching)."""
    repo_root = Path(repo_root).resolve()
    pkg = {}
    if (repo_root / "package.json").exists():
        try:
            pkg = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
        except Exception:
            pkg = {}
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    framework = "electron" if "electron" in deps else "react-native" if ("react-native" in deps or "expo" in deps) else "unknown"
    inv = CodeInventory(framework=framework)
    app_name = pkg.get("productName") or (pkg.get("build") or {}).get("productName") or pkg.get("name") or "app"

    parsed: dict[str, tuple[Path, Node]] = {}
    for path in _files(repo_root):
        try:
            parsed[str(path.relative_to(repo_root))] = (path, _parse(path))
        except Exception as exc:
            inv.warnings.append(f"{path}: {type(exc).__name__}: {exc}")
    inv.files_parsed = len(parsed)

    imports = {rel: _imports(root, path, repo_root) for rel, (path, root) in parsed.items()}
    screen_file: dict[str, str] = {}
    for rel, (path, root) in parsed.items():
        for route, target in _screen_routes(root, rel, imports[rel]):
            if route.path not in screen_file:
                inv.routes.append(route)
                screen_file[route.path] = target or rel
        if (er := _expo_route(rel)) is not None and "react-native" in framework:
            inv.routes.append(CodeRoute(path=er, element=Path(rel).stem, protected=not AUTH_SCREEN_RE.search(er), file=rel, line=1))
            screen_file[er] = rel
    inv.page_files = dict(screen_file)

    # files reachable from each screen through local imports (components it renders)
    navigator_files = {r.file for r in inv.routes}
    screen_files_set = set(screen_file.values())
    owners: dict[str, list[str]] = {}
    for screen, start in screen_file.items():
        seen, frontier = {start}, [start]
        for _ in range(3):
            nxt = []
            for f in frontier:
                for target in imports.get(f, {}).values():
                    if target in parsed and target not in seen and target not in screen_files_set and target not in navigator_files:
                        seen.add(target)
                        nxt.append(target)
            frontier = nxt
        for f in seen:
            owners.setdefault(f, []).append(screen)

    texts_by_screen: dict[str, set[str]] = {s: set() for s in screen_file}
    registered = {r.path for r in inv.routes}
    for rel, (path, root) in parsed.items():
        source = path.read_text(encoding="utf-8", errors="replace")
        bodies = _function_bodies(root)
        routes = owners.get(rel) or (["(layout)"] if rel in navigator_files else ["(unrouted)"])
        for m in API_RE.finditer(source):
            inv.api_calls.append(ApiCall(file=rel, function=None, method=m.group(1).upper(), path=m.group(2)))
        for m in FETCH_RE.finditer(source):
            inv.api_calls.append(ApiCall(file=rel, function=None, method=(m.group(2) or "GET").upper(), path=m.group(1)))
        for m in STORAGE_RE.finditer(source):
            op = "setItem" if m.group(2).startswith("set") else "removeItem" if m.group(2) in ("removeItem", "delete") else "getItem"
            inv.storage.append(StorageFact(file=rel, line=source[: m.start()].count("\n") + 1, op=op, key=m.group(3)))
        for m in TIMER_RE.finditer(source):
            inv.timers.append(TimerFact(file=rel, line=source[: m.start()].count("\n") + 1, kind=m.group(1), ms=int(m.group(2)), function=None))
        if framework == "electron":
            for el in _electron_menus(root, rel, app_name):
                el.key = f"{el.route}|{el.kind}|{el.label}|{rel}:{el.line}"
                inv.elements.append(el)
            continue
        for screen in routes:
            if screen in texts_by_screen:
                texts_by_screen[screen].update(t for t in _children_text(root) if 1 < len(t) < 80)
        for n in _walk(root):
            if n.type not in ("jsx_self_closing_element", "jsx_opening_element"):
                continue
            tag = _tag_name(n)
            attrs = _attr_map(n)
            short = tag.split(".")[-1]
            kind = RN_KIND.get(short)
            if kind is None and "onPress" in attrs and short[:1].isupper():
                kind = "button"
            if kind is None and "onChangeText" in attrs and short[:1].isupper():
                kind = "input"  # a design-system text field (<TextField label onChangeText>) wraps a TextInput
            if kind is None:
                continue
            el_node = n.parent if n.type == "jsx_opening_element" else n
            label, alts = _label(short, attrs, el_node)
            event = "onPress" if "onPress" in attrs else "onChangeText" if "onChangeText" in attrs else "onValueChange" if "onValueChange" in attrs else None
            handler, src = _handler_source(attrs, bodies, event) if event else ("", "")
            plain = _plain(attrs)
            if "testID" in plain:
                plain["id"] = plain["testID"]
            if kind == "input":
                plain["secure"] = bool(attrs.get("secureTextEntry") and attrs["secureTextEntry"][1] not in (False, "false"))
            opens = (["confirm"] if ALERT_RE.search(src) else []) + (["toggle"] if re.search(r"set\w*(Open|Visible|Show|Expanded)\w*\(", src) else []) + (["toast"] if TOAST_RE.search(src) else [])
            in_cond = False
            cur = el_node.parent
            while cur is not None and cur.type not in ("function_declaration", "arrow_function", "method_definition"):
                if cur.type in ("ternary_expression", "binary_expression") or (cur.type == "jsx_expression" and "&&" in _text(cur)[:200]):
                    in_cond = True
                    break
                cur = cur.parent
            repeated = any(a.type == "call_expression" and _text(a.child_by_field_name("function")).endswith(".map") for a in _ancestors(el_node, 8))
            for screen in routes:
                el = CodeElement(file=rel, line=n.start_point[0] + 1, tag=short, kind=kind, label=label, label_alternatives=alts, handler=handler or None, event=event,
                                 attrs=dict(plain), route=screen, conditional=in_cond, repeated=repeated, shared=len(routes) > 1,
                                 navigates_to=list(dict.fromkeys(a or b for a, b in NAV_RE.findall(src)))[:5], opens=opens, calls=re.findall(r"\b([a-zA-Z_]\w*)\s*\(", src)[:30])
                el.attrs["writes"] = sorted({(m.group(1) or m.group(2) or "").upper() for m in WRITE_RE.finditer(src)})
                el.attrs["session_ending"] = bool(LOGOUT_RE.search(src) or LOGOUT_RE.search(handler or ""))
                el.key = f"{screen}|{kind}|{label or handler or ''}|{rel}:{el.line}"
                inv.elements.append(el)
        for el in _data_items(root, rel, routes, registered):
            inv.elements.append(el)
    return inv, texts_by_screen


def _data_items(root: Node, rel: str, routes: list[str], registered: set[str]) -> list[CodeElement]:
    """Controls rendered from data: [{ title: 'Settings', route: 'Settings' }, { label: 'Device', onPress: () => navigation.navigate('ConnectDevice') }].
    An entry counts when it has a label and a target screen or a handler."""
    out = []
    for obj in _walk(root):
        if obj.type != "object" or obj.parent is None or obj.parent.type != "array":
            continue
        props: dict[str, Node] = {}
        for pair in obj.children:
            if pair.type == "pair" and pair.child_by_field_name("key") is not None and pair.child_by_field_name("value") is not None:
                props[_text(pair.child_by_field_name("key")).strip("'\"")] = pair.child_by_field_name("value")
        label = next((_string_value(props[k]) for k in ITEM_LABEL_KEYS if k in props and props[k].type in ("string", "template_string")), None)
        if not label:
            continue
        handler = next((props[k] for k in ITEM_HANDLER_KEYS if k in props), None)
        src = _text(handler) if handler is not None else ""
        targets = [a or b for a, b in NAV_RE.findall(src)]
        for k in ITEM_ROUTE_KEYS:
            v = _string_value(props[k]) if k in props and props[k].type == "string" else None
            if v and v in registered:
                targets.append(v)
        if not targets and handler is None:
            continue
        opens = (["confirm"] if ALERT_RE.search(src) else []) + (["toast"] if TOAST_RE.search(src) else []) + (["sheet"] if re.search(r"set\w*(Sheet|Modal|Open|Visible)\w*\(", src) else [])
        for screen in routes:
            el = CodeElement(file=rel, line=obj.start_point[0] + 1, tag="item", kind="button", label=label, label_alternatives=[], handler="inline" if handler is not None else None,
                             event="onPress", attrs={"writes": sorted({(m.group(1) or m.group(2) or "").upper() for m in WRITE_RE.finditer(src)}),
                                                     "session_ending": bool(LOGOUT_RE.search(src) or LOGOUT_RE.search(label))},
                             route=screen, conditional=False, repeated=True, shared=len(routes) > 1, navigates_to=list(dict.fromkeys(targets))[:5], opens=opens,
                             calls=re.findall(r"\b([a-zA-Z_]\w*)\s*\(", src)[:30])
            el.key = f"{screen}|button|{label}|{rel}:{el.line}"
            out.append(el)
    return out


def _ancestors(n: Node, limit: int) -> list[Node]:
    out, cur = [], n.parent
    while cur is not None and len(out) < limit:
        out.append(cur)
        cur = cur.parent
    return out


def app_intent(el: CodeElement) -> dict:
    """What the code says this control should do, in the shape the report expects."""
    expected, api_calls = [], []
    writes = el.attrs.get("writes") or []
    for target in el.navigates_to:
        expected.append(f"Navigates to screen {target}")
    if "confirm" in el.opens:
        expected.append("Shows a confirmation alert; nothing changes until confirmed")
    if "toggle" in el.opens:
        expected.append("Opens or closes a menu, modal or disclosure")
    if el.attrs.get("session_ending"):
        expected.append("Signs the user out and returns to the sign-in screen")
    for m in writes:
        api_calls.append({"method": m, "path": "(from handler)"})
        expected.append(f"Sends a {m} request")
    if el.kind == "input":
        if el.attrs.get("secure"):
            expected.append("Masks what is typed (secureTextEntry)")
        if el.attrs.get("maxLength"):
            expected.append(f"Accepts at most {el.attrs['maxLength']} characters")
        if el.attrs.get("keyboardType"):
            expected.append(f"Uses the {el.attrs['keyboardType']} keyboard")
    if el.kind == "menuitem" and el.attrs.get("accelerator"):
        expected.append(f"Shortcut {el.attrs['accelerator']}")
    if not expected:
        expected.append("Responds without crashing or freezing the app")
    return {"expected": expected, "api_calls": api_calls, "navigates_to": el.navigates_to, "opens": el.opens, "handler": el.handler or "",
            "session_ending": bool(el.attrs.get("session_ending")), "destructive": "DELETE" in writes or "confirm" in el.opens and bool(re.search(r"delete|remove", el.label or "", re.I))}
