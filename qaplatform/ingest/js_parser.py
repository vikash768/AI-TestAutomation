"""Static extraction of UI intent from a React / JSX codebase.

Uses tree-sitter to walk every JS/JSX/TS/TSX file and pull out:

  routes        <Route path element> trees (with guard and layout ancestry)
  elements      every interactive JSX element: tag, kind, label, handler,
                where it lives, whether it is conditional or repeated
  api_calls     request(SERVICE, 'METHOD', '/path') and fetch(...) sites
  timers        setTimeout / setInterval with their delays
  storage       localStorage keys read and written
  wrappers      components whose root is a native control (Button -> button)

The output is deterministic. Nothing here talks to an LLM.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

JS_LANG = Language(tsjs.language())
TS_LANG = Language(tsts.language_typescript())
TSX_LANG = Language(tsts.language_tsx())

NATIVE_KINDS = {
    "button": "button",
    "a": "link",
    "input": "input",
    "select": "select",
    "textarea": "textarea",
    "form": "form",
    "summary": "button",
    "option": None,
}
LINK_COMPONENTS = {"Link", "NavLink"}
ROLE_KIND = {
    "button": "button",
    "link": "link",
    "menuitem": "menuitem",
    "menuitemcheckbox": "menuitem",
    "tab": "tab",
    "switch": "switch",
    "checkbox": "checkbox",
    "radio": "radio",
    "combobox": "select",
    "listbox": "select",
    "textbox": "input",
    "searchbox": "input",
    "slider": "input",
    "spinbutton": "input",
}
INPUT_TYPE_KIND = {"checkbox": "checkbox", "radio": "radio", "submit": "button", "button": "button", "reset": "button", "file": "file", "hidden": None}
EVENT_ATTRS = {"onClick", "onSubmit", "onChange", "onInput", "onKeyDown", "onKeyUp", "onToggle", "onPointerDown", "onMouseDown", "onDoubleClick"}
GUARD_RE = re.compile(r"(Protected|Private|RequireAuth|Guard|AuthRoute|Authenticated)", re.I)
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "coverage", "__live__", "public", ".next", "out"}
SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs"}
NAV_KEYS = ("to", "path", "href")


@dataclass
class CodeElement:
    file: str
    line: int
    tag: str
    kind: str
    label: str | None = None
    label_alternatives: list[str] = field(default_factory=list)
    handler: str | None = None
    event: str | None = None
    attrs: dict = field(default_factory=dict)
    href: str | None = None
    component: str | None = None
    route: str | None = None
    repeated: bool = False
    conditional: bool = False
    shared: bool = False
    in_form: bool = False
    form_handler: str | None = None
    navigates_to: list[str] = field(default_factory=list)
    opens: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    deferred_calls: list[str] = field(default_factory=list)
    ancestors: list[str] = field(default_factory=list)
    pos: int = 0
    key: str = ""


@dataclass
class CodeRoute:
    path: str
    element: str | None
    protected: bool = False
    layouts: list[str] = field(default_factory=list)
    file: str = ""
    line: int = 0


@dataclass
class ApiCall:
    file: str
    function: str | None
    method: str
    path: str
    line: int = 0


@dataclass
class TimerFact:
    file: str
    line: int
    kind: str
    ms: int | None
    function: str | None
    calls: list[str] = field(default_factory=list)


@dataclass
class StorageFact:
    file: str
    line: int
    op: str
    key: str


@dataclass
class NavigateFact:
    file: str
    line: int
    component: str | None
    target: str


@dataclass
class CodeInventory:
    framework: str = ""
    routes: list[CodeRoute] = field(default_factory=list)
    elements: list[CodeElement] = field(default_factory=list)
    api_calls: list[ApiCall] = field(default_factory=list)
    timers: list[TimerFact] = field(default_factory=list)
    storage: list[StorageFact] = field(default_factory=list)
    navigates: list[NavigateFact] = field(default_factory=list)
    wrappers: dict = field(default_factory=dict)
    files_parsed: int = 0
    warnings: list[str] = field(default_factory=list)
    page_files: dict = field(default_factory=dict)  # route -> file

    def to_dict(self) -> dict:
        return {
            "framework": self.framework,
            "routes": [asdict(r) for r in self.routes],
            "elements": [asdict(e) for e in self.elements],
            "api_calls": [asdict(a) for a in self.api_calls],
            "timers": [asdict(t) for t in self.timers],
            "storage": [asdict(s) for s in self.storage],
            "navigates": [asdict(n) for n in self.navigates],
            "wrappers": self.wrappers,
            "files_parsed": self.files_parsed,
            "warnings": self.warnings,
            "page_files": self.page_files,
        }


# ---------------------------------------------------------------------------
# tree helpers
# ---------------------------------------------------------------------------

def _text(n: Node | None) -> str:
    return n.text.decode("utf-8", errors="replace") if n is not None else ""


def _children_of_type(n: Node, t: str) -> list[Node]:
    return [c for c in n.children if c.type == t]


def _walk(n: Node):
    stack = [n]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _string_value(n: Node) -> str | None:
    if n.type == "string":
        frags = _children_of_type(n, "string_fragment")
        return "".join(_text(f) for f in frags) if frags else ""
    if n.type == "template_string":
        return _text(n)[1:-1]
    return None


def _string_literals(n: Node) -> list[str]:
    out = []
    for node in _walk(n):
        if node.type == "string":
            v = _string_value(node)
            if v is not None and v.strip():
                out.append(v.strip())
        elif node.type == "template_string":
            out.append(_text(node)[1:-1])
    return out


def _tag_name(opening: Node) -> str:
    name = opening.child_by_field_name("name")
    return _text(name) if name is not None else ""


def _attr_map(opening: Node) -> dict:
    attrs: dict = {}
    for a in _children_of_type(opening, "jsx_attribute"):
        name_node = a.children[0] if a.children else None
        name = _text(name_node)
        if len(a.children) == 1:
            attrs[name] = ("bool", True)
            continue
        value = a.children[-1]
        if value.type == "string":
            attrs[name] = ("str", _string_value(value) or "")
        elif value.type == "jsx_expression":
            inner = value.children[1] if len(value.children) > 2 else None
            if inner is None:
                attrs[name] = ("expr", "")
            elif inner.type == "identifier":
                attrs[name] = ("ident", _text(inner))
            elif inner.type in ("arrow_function", "function_expression", "function"):
                attrs[name] = ("inline", _text(inner))
            elif inner.type == "string":
                attrs[name] = ("str", _string_value(inner) or "")
            elif inner.type == "template_string":
                attrs[name] = ("template", _text(inner)[1:-1])
            elif inner.type == "ternary_expression":
                attrs[name] = ("alts", _string_literals(inner))
            elif inner.type == "member_expression":
                attrs[name] = ("member", _text(inner))
            elif inner.type in ("true", "false"):
                attrs[name] = ("bool", inner.type == "true")
            elif inner.type == "spread_element":
                attrs["..."] = ("spread", _text(inner))
            else:
                attrs[name] = ("expr", _text(inner))
    for c in opening.children:
        if c.type == "jsx_expression" and any(g.type == "spread_element" for g in c.children):
            attrs["..."] = ("spread", _text(c))
    return attrs


def _plain(attrs: dict) -> dict:
    """Collapse (kind, value) pairs into a JSON-friendly dict."""
    out = {}
    for k, (kind, v) in attrs.items():
        if k == "...":
            out["spread"] = True
        elif kind in ("str", "template", "bool", "member", "ident"):
            out[k] = v
        elif kind == "alts":
            out[k] = " | ".join(v)
        elif kind == "inline":
            out[k] = "inline"
        else:
            out[k] = str(v)[:80]
    return out


def _enclosing_function_name(n: Node) -> str | None:
    cur = n.parent
    while cur is not None:
        if cur.type == "function_declaration":
            return _text(cur.child_by_field_name("name"))
        if cur.type in ("arrow_function", "function_expression", "function") and cur.parent is not None:
            p = cur.parent
            if p.type == "variable_declarator":
                return _text(p.child_by_field_name("name"))
            if p.type == "call_expression":  # forwardRef(function Button(...))
                inner = cur.child_by_field_name("name")
                if inner is not None:
                    return _text(inner)
                gp = p.parent
                if gp is not None and gp.type == "variable_declarator":
                    return _text(gp.child_by_field_name("name"))
        cur = cur.parent
    return None


def _is_conditional(n: Node, stop_at: Node | None) -> bool:
    cur = n.parent
    while cur is not None and cur is not stop_at:
        if cur.type == "ternary_expression":
            return True
        if cur.type == "binary_expression" and any(c.type == "&&" or _text(c) == "&&" for c in cur.children):
            return True
        if cur.type in ("if_statement",):
            return True
        if cur.type in ("function_declaration", "arrow_function", "function_expression"):
            if cur.parent is not None and cur.parent.type == "arguments":
                cur = cur.parent  # a .map / callback: keep looking at the enclosing JSX
                continue
            return False
        cur = cur.parent
    return False


def _map_context(n: Node) -> tuple[str | None, str | None]:
    src, param, _ = _map_context_node(n)
    return src, param


def _map_context_node(n: Node) -> tuple[str | None, str | None, Node | None]:
    """If inside xs.map((item) => ...), return (xs expression text, item param name, xs node)."""
    cur = n.parent
    while cur is not None:
        if cur.type == "call_expression":
            fn = cur.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression" and _text(fn.child_by_field_name("property")) == "map":
                args = cur.child_by_field_name("arguments")
                param = None
                if args is not None:
                    for a in args.children:
                        if a.type in ("arrow_function", "function_expression"):
                            params = a.child_by_field_name("parameters") or a.child_by_field_name("parameter")
                            if params is not None:
                                ids = [c for c in _walk(params) if c.type in ("identifier", "shorthand_property_identifier_pattern")]
                                param = _text(ids[0]) if ids else None
                            break
                obj = fn.child_by_field_name("object")
                return _text(obj), param, obj
        cur = cur.parent
    return None, None, None


def _array_items(value: Node) -> list:
    """Items of an array literal: strings, [value, label] pairs, {label, to} objects; spreads are searched for nested arrays."""
    items: list = []
    for item in value.children:
        if item.type == "string":
            items.append(_string_value(item))
        elif item.type == "template_string":
            items.append(_text(item)[1:-1])
        elif item.type == "array":
            strs = [(_string_value(c) if c.type == "string" else _text(c)[1:-1]) for c in item.children if c.type in ("string", "template_string")]
            if strs:
                items.append({"value": strs[0], "label": strs[-1]})
        elif item.type == "object":
            obj = {}
            for pair in _children_of_type(item, "pair"):
                k = _text(pair.child_by_field_name("key")).strip("'\"")
                v = pair.child_by_field_name("value")
                if v is not None and v.type in ("string", "template_string"):
                    obj[k] = _string_value(v)
                elif v is not None and v.type == "identifier":
                    obj[k] = f"${{{_text(v)}}}"
            if obj:
                items.append(obj)
        elif item.type == "spread_element":
            for inner in _walk(item):
                if inner.type == "array" and inner.parent is not None and inner.parent.type != "array":
                    items.extend(_array_items(inner))
    return items


def _inside_call_argument(n: Node) -> bool:
    """JSX passed as a function argument (openDrawer({content: <Form/>})) renders later, not at rest."""
    cur = n.parent
    while cur is not None:
        if cur.type == "arguments":
            return True
        if cur.type in ("return_statement", "function_declaration", "arrow_function", "function_expression", "program"):
            return False
        cur = cur.parent
    return False


def _wrapping_label_text(n: Node) -> str | None:
    cur = n.parent
    while cur is not None:
        if cur.type == "jsx_element":
            o = cur.child_by_field_name("open_tag")
            if o is not None and _tag_name(o) == "label":
                text = " ".join(re.sub(r"\s+", " ", _text(c)).strip() for c in cur.children if c.type == "jsx_text").strip()
                return text or None
            return None
        cur = cur.parent
    return None


def _capitalized_ancestors(n: Node) -> list[str]:
    out = []
    cur = n.parent
    while cur is not None and len(out) < 6:
        if cur.type == "jsx_element":
            o = cur.child_by_field_name("open_tag")
            name = _tag_name(o) if o is not None else ""
            if name and name[0].isupper():
                out.append(name)
        cur = cur.parent
    return out


def _default_params(fnode: Node) -> dict[str, str]:
    params = fnode.child_by_field_name("parameters")
    if params is None:
        return {}
    return {m.group(1): m.group(2) for m in re.finditer(r"\b(\w+)\s*=\s*['\"]([^'\"]*)['\"]", _text(params))}


def _substitute(label: str | None, values: dict) -> str | None:
    if not label or not values:
        return label
    def rep(m):
        name = m.group(1).strip()
        v = values.get(name)
        return str(v) if isinstance(v, str) else m.group(0)
    return re.sub(r"\$?\{\s*([A-Za-z_]\w*)\s*\}", rep, label)


def _enclosing_form(n: Node) -> Node | None:
    cur = n.parent
    while cur is not None:
        if cur.type == "jsx_element":
            opening = cur.child_by_field_name("open_tag")
            if opening is not None and _tag_name(opening) == "form":
                return opening
        cur = cur.parent
    return None


FUNCTION_TYPES = ("arrow_function", "function_expression", "function", "function_declaration")


def _walk_shallow(n: Node):
    """Walk n without descending into nested function bodies."""
    stack = [n]
    while stack:
        node = stack.pop()
        yield node
        for c in reversed(node.children):
            if c.type in FUNCTION_TYPES and c is not n:
                continue
            stack.append(c)


def _call_names(n: Node, shallow: bool = False) -> list[str]:
    out = []
    for node in (_walk_shallow(n) if shallow else _walk(n)):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                out.append(_text(fn))
    return out


def _navigate_targets(n: Node) -> list[str]:
    out = []
    for node in _walk(n):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and _text(fn) in ("navigate", "router.push", "history.push", "router.replace"):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for a in args.children:
                        if a.type in ("(", ")", ","):
                            continue
                        v = _string_value(a) if a.type in ("string", "template_string") else None
                        if v:
                            out.append(v)
                        break
    return out


# ---------------------------------------------------------------------------
# per-file parser
# ---------------------------------------------------------------------------

class FileFacts:
    def __init__(self, rel: str):
        self.rel = rel
        self.imports: dict[str, str] = {}  # local name -> module specifier
        self.const_arrays: dict[str, list] = {}
        self.const_values: dict[str, str | int] = {}
        self.functions: dict[str, Node] = {}
        self.elements: list[CodeElement] = []
        self.routes: list[CodeRoute] = []
        self.api_calls: list[ApiCall] = []
        self.timers: list[TimerFact] = []
        self.storage: list[StorageFact] = []
        self.navigates: list[NavigateFact] = []
        self.wrappers: dict[str, str] = {}
        self.wrapper_info: dict[str, dict] = {}
        self.label_for: dict[str, str] = {}
        self.derived_arrays: set[str] = set()
        self.conditional_components: set[str] = set()
        self.func_defaults: dict[str, dict] = {}
        self.early_ranges: list[tuple[int, int]] = []
        self.exports: set[str] = set()


def _lang_for(path: Path) -> Language:
    if path.suffix == ".tsx":
        return TSX_LANG
    if path.suffix == ".ts":
        return TS_LANG
    return JS_LANG


def parse_file(path: Path, root: Path) -> FileFacts:
    rel = str(path.relative_to(root))
    facts = FileFacts(rel)
    source = path.read_bytes()
    tree = Parser(_lang_for(path)).parse(source)
    rootn = tree.root_node

    # imports, consts, functions
    for n in rootn.children:
        if n.type == "import_statement":
            spec = None
            for c in n.children:
                if c.type == "string":
                    spec = _string_value(c)
            if not spec:
                continue
            for node in _walk(n):
                if node.type == "import_specifier":
                    local = node.child_by_field_name("alias") or node.child_by_field_name("name")
                    facts.imports[_text(local)] = spec
                elif node.type == "identifier" and node.parent is not None and node.parent.type == "import_clause":
                    facts.imports[_text(node)] = spec
                elif node.type == "namespace_import":
                    ids = [c for c in node.children if c.type == "identifier"]
                    if ids:
                        facts.imports[_text(ids[-1])] = spec
        if n.type in ("lexical_declaration", "variable_declaration") or (
            n.type == "export_statement" and any(c.type in ("lexical_declaration", "variable_declaration") for c in n.children)
        ):
            decl = n if n.type != "export_statement" else next(c for c in n.children if c.type in ("lexical_declaration", "variable_declaration"))
            for d in _children_of_type(decl, "variable_declarator"):
                name = _text(d.child_by_field_name("name"))
                value = d.child_by_field_name("value")
                if value is None:
                    continue
                if value.type == "array":
                    items = _array_items(value)
                    if items:
                        facts.const_arrays[name] = items
                elif value.type in ("string", "template_string"):
                    facts.const_values[name] = _string_value(value) or ""
                elif value.type == "number":
                    try:
                        facts.const_values[name] = int(float(_text(value)))
                    except ValueError:
                        pass
                elif value.type in ("binary_expression", "parenthesized_expression") and re.fullmatch(r"[\d\s+\-*/()._A-Z]+", _text(value)):
                    try:
                        known = {k: v for k, v in facts.const_values.items() if isinstance(v, int)}
                        facts.const_values[name] = int(eval(_text(value).replace("_", ""), {"__builtins__": {}}, known))
                    except Exception:
                        pass
                elif value.type in ("arrow_function", "function_expression"):
                    facts.functions[name] = value
                elif value.type == "call_expression":  # forwardRef(function X(){})
                    for c in _walk(value):
                        if c.type in ("function_expression", "function", "arrow_function"):
                            facts.functions[name] = c
                            break
            if n.type == "export_statement":
                for d in _children_of_type(decl, "variable_declarator"):
                    facts.exports.add(_text(d.child_by_field_name("name")))
        if n.type == "function_declaration":
            facts.functions[_text(n.child_by_field_name("name"))] = n
        if n.type == "export_statement":
            for c in n.children:
                if c.type == "function_declaration":
                    name = _text(c.child_by_field_name("name"))
                    facts.functions[name] = c
                    facts.exports.add(name)
                    if "default" in [_text(x) for x in n.children]:
                        facts.exports.add("default:" + name)
                elif c.type == "identifier" and "default" in [_text(x) for x in n.children]:
                    facts.exports.add("default:" + _text(c))

    # nested function declarations and arrow functions assigned inside components
    for node in _walk(rootn):
        if node.type == "function_declaration" and node.parent is not None and node.parent.type not in ("program", "export_statement"):
            facts.functions.setdefault(_text(node.child_by_field_name("name")), node)
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name")
            if value is not None and name_node is not None and value.type in ("arrow_function", "function_expression"):
                facts.functions.setdefault(_text(name_node), value)

    # arrays derived from constant arrays: const tabs = cond ? [...TABS, EXTRA] : TABS
    for node in _walk(rootn):
        if node.type != "variable_declarator":
            continue
        value = node.child_by_field_name("value")
        name_node = node.child_by_field_name("name")
        if value is None or name_node is None or name_node.type != "identifier":
            continue
        name = _text(name_node)
        if name in facts.const_arrays or value.type not in ("ternary_expression", "array", "binary_expression"):
            continue
        ids = [_text(c) for c in _walk(value) if c.type == "identifier"]
        items: list = []
        for ident in ids:
            if ident in facts.const_arrays:
                items.extend(facts.const_arrays[ident])
            elif isinstance(facts.const_values.get(ident), str):
                items.append(facts.const_values[ident])
        if items and any(i in facts.const_arrays for i in ids):
            deduped: list = []
            for it in items:
                if it not in deduped:
                    deduped.append(it)
            facts.const_arrays[name] = deduped
            facts.derived_arrays.add(name)

    # components that `return null` early render conditionally (modals, toasts, pagers)
    conditional_components: set[str] = set()
    for fname, fnode in facts.functions.items():
        facts.func_defaults[fname] = _default_params(fnode)
        returns = [n for n in _walk_shallow(fnode) if n.type == "return_statement"]
        for node in returns:
            if any(c.type == "null" for c in node.children):
                conditional_components.add(fname)
                break
        # a component with several returns renders the non-final ones only in some states
        for node in returns[:-1]:
            facts.early_ranges.append((node.start_byte, node.end_byte))
    facts.conditional_components = conditional_components

    # <label htmlFor="id">Text</label> -> id: Text
    for node in _walk(rootn):
        if node.type == "jsx_element":
            opening = node.child_by_field_name("open_tag")
            if opening is not None and _tag_name(opening) == "label":
                a = _attr_map(opening)
                target = a.get("htmlFor") or a.get("for")
                if target and target[0] == "str":
                    text = " ".join(re.sub(r"\s+", " ", _text(c)).strip() for c in node.children if c.type == "jsx_text").strip()
                    if text:
                        facts.label_for[str(target[1])] = text

    # wrapper components: a function whose root JSX is an interactive control
    for fname, fnode in list(facts.functions.items()):
        if not fname or not fname[0].isupper():
            continue
        root = _root_jsx(fnode)
        if root is None:
            continue
        opening = root.child_by_field_name("open_tag") if root.type == "jsx_element" else root
        if opening is None:
            continue
        rtag = _tag_name(opening)
        rattrs = _attr_map(opening)
        role = rattrs.get("role", (None, None))[1] if "role" in rattrs else None
        kind = ROLE_KIND.get(str(role)) if role else None
        if kind is None and rtag and rtag[0].islower():
            kind = NATIVE_KINDS.get(rtag)
            if rtag == "input":
                itype = rattrs.get("type", ("str", "text"))[1]
                kind = INPUT_TYPE_KIND.get(str(itype), kind) if isinstance(itype, str) else kind
        if kind is None and not (rtag and rtag[0].isupper()):
            continue
        if kind == "form" or rtag in ("Fragment",):
            continue  # a component that renders a form is a page section, not a control wrapper
        if kind is None and any(n is not root and n.type in ("jsx_element", "jsx_self_closing_element") for n in _walk(root)):
            continue  # a composite (<Menu>{() => <Item/>...}</Menu>) is content, not a thin wrapper around a control
        subtree_text = _text(root)
        label_prop = "label" if "{label}" in subtree_text else None
        default_label = _default_params(fnode).get("label")
        root_label, _ = _label_for(root, rattrs, rtag)
        children_conditional = False
        for node in _walk_shallow(fnode):
            if node.type == "identifier" and _text(node) == "children":
                cur = node.parent
                while cur is not None and cur is not fnode:
                    if cur.type == "ternary_expression" or (cur.type == "binary_expression" and any(_text(c) == "&&" for c in cur.children)):
                        children_conditional = True
                        break
                    cur = cur.parent
        facts.wrapper_info[fname] = {
            "kind": kind,
            "root_tag": rtag,
            "root_line": root.start_point[0] + 1,
            "label_prop": label_prop,
            "default_label": default_label,
            "root_label": root_label if root_label and root_label != "{children}" else None,
            "children_label": "{children}" in subtree_text or "..." in rattrs,
            "children_conditional": children_conditional,
            "defaults": _default_params(fnode),
        }
        if kind:
            facts.wrappers[fname] = kind

    # JSX elements
    for node in _walk(rootn):
        if node.type not in ("jsx_element", "jsx_self_closing_element"):
            continue
        opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
        if opening is None:
            continue
        tag = _tag_name(opening)
        attrs = _attr_map(opening)
        line = node.start_point[0] + 1

        if tag == "Route":
            r = _route_from(node, opening, attrs, facts)
            if r is not None:
                facts.routes.append(r)
            continue
        if tag == "Navigate":
            to = attrs.get("to")
            if to and to[0] in ("str", "template"):
                facts.navigates.append(NavigateFact(rel, line, _enclosing_function_name(node), to[1]))
            continue

        role = attrs.get("role", (None, None))[1] if attrs.get("role") else None
        has_event = any(k in attrs for k in EVENT_ATTRS)
        native_kind = NATIVE_KINDS.get(tag) if tag and tag[0].islower() else None
        kind: str | None = None
        if role in ROLE_KIND:
            kind = ROLE_KIND[role]
        elif native_kind:
            kind = native_kind
            if tag == "input":
                itype = attrs.get("type", ("str", "text"))[1]
                if isinstance(itype, str) and itype in INPUT_TYPE_KIND:
                    kind = INPUT_TYPE_KIND[itype]
        elif tag in LINK_COMPONENTS:
            kind = "link"
        elif has_event:
            kind = "clickable"
        if kind is None and tag and tag[0].isupper():
            kind = "component"  # resolved later against wrappers
        if kind is None:
            continue
        if tag == "form" and not has_event:
            continue

        # A wrapper definition (<button {...props}>) is a kind, not an instance.
        if "..." in attrs and tag and tag[0].islower():
            fn = _enclosing_function_name(node)
            if fn and native_kind:
                facts.wrappers[fn] = kind
            continue

        label, alts = _label_for(node, attrs, tag)
        if not label and tag in ("input", "select", "textarea"):
            label = _wrapping_label_text(node)
        handler = None
        event = None
        for ev in ("onClick", "onSubmit", "onChange", "onToggle", "onPointerDown", "onMouseDown", "onKeyDown", "onInput"):
            if ev in attrs:
                event = ev
                hk, hv = attrs[ev]
                handler = hv if hk == "ident" else ("inline" if hk == "inline" else str(hv)[:60])
                break
        href = None
        for key in ("href", "to"):
            if key in attrs and attrs[key][0] in ("str", "template"):
                href = attrs[key][1]
        map_src, map_param, map_node = _map_context_node(node)
        form = _enclosing_form(node)
        form_handler = None
        if form is not None:
            fa = _attr_map(form).get("onSubmit")
            if fa and fa[0] == "ident":
                form_handler = fa[1]
            elif fa:
                form_handler = "inline"

        navigates, opens, calls, deferred = _handler_facts(attrs, facts, event)
        el = CodeElement(
            file=rel,
            line=line,
            tag=tag,
            kind=kind,
            label=label,
            label_alternatives=alts,
            handler=handler,
            event=event,
            attrs=_plain(attrs),
            href=href,
            component=_enclosing_function_name(node),
            repeated=map_src is not None,
            conditional=_is_conditional(node, None) or _inside_call_argument(node),
            ancestors=_capitalized_ancestors(node),
            pos=node.start_byte,
            in_form=form is not None,
            form_handler=form_handler,
            navigates_to=navigates + ([href] if href and href.startswith("/") else []),
            opens=opens,
            calls=calls,
            deferred_calls=deferred,
        )

        expanded = False
        if map_src and map_param:
            src_name = map_src.split(".")[0]
            items = facts.const_arrays.get(src_name)
            if items is None and map_node is not None and map_node.type == "array":
                items = _array_items(map_node) or None
            link_like = kind == "link" or tag in LINK_COMPONENTS or tag == "a" or "to" in attrs or "href" in attrs
            if items:
                for item in items:
                    clone = CodeElement(**{**asdict(el)})
                    clone.repeated = False
                    if src_name in facts.derived_arrays:
                        clone.conditional = True
                    if isinstance(item, dict):
                        if not clone.label or "{" in clone.label:
                            clone.label = item.get("label") or item.get("name") or item.get("title") or clone.label
                        if link_like:
                            for nk in NAV_KEYS:
                                if item.get(nk):
                                    clone.href = item[nk]
                                    clone.navigates_to = [item[nk]]
                    elif not clone.label or "{" in clone.label:
                        clone.label = str(item)
                    facts.elements.append(clone)
                expanded = True
        if not expanded:
            facts.elements.append(el)

    # components that only ever render inside a call argument (drawer / modal content) are conditional too
    deferred_components: set[str] = set()
    for node in _walk(rootn):
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
            name = _tag_name(opening) if opening is not None else ""
            if name and name[0].isupper() and name in facts.functions and _inside_call_argument(node):
                deferred_components.add(name)
    for node in _walk(rootn):
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
            name = _tag_name(opening) if opening is not None else ""
            if name in deferred_components and not _inside_call_argument(node):
                deferred_components.discard(name)

    # components whose every usage sits inside an early (non-final) return are conditional too
    early_only: set[str] = set()
    usage_outside: set[str] = set()
    for node in _walk(rootn):
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
            name = _tag_name(opening) if opening is not None else ""
            if not (name and name[0].isupper() and name in facts.functions):
                continue
            if any(a <= node.start_byte < b for a, b in facts.early_ranges):
                early_only.add(name)
            else:
                usage_outside.add(name)
    early_only -= usage_outside

    repeated_components: set[str] = set()
    for node in _walk(rootn):
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
            name = _tag_name(opening) if opening is not None else ""
            if name and name[0].isupper() and name in facts.functions and _map_context(node)[0] is not None:
                repeated_components.add(name)

    root_lines = {info["root_line"] for info in facts.wrapper_info.values()}
    kept = []
    for el in facts.elements:
        if el.line in root_lines and el.tag:
            continue
        if el.component in facts.conditional_components or el.component in deferred_components or el.component in early_only:
            el.conditional = True
        if el.component in repeated_components:
            el.repeated = True
        if any(a <= el.pos < b for a, b in facts.early_ranges):
            el.conditional = True
        if el.label and el.component in facts.func_defaults:
            el.label = _substitute(el.label, facts.func_defaults[el.component])
        if el.label and ("{" in el.label or "${" in el.label):
            el.conditional = True  # data-driven label; matched by pattern, excluded from the strict coverage figure
        if el.kind == "clickable" and not el.label:
            el.conditional = True
        if not el.label and isinstance(el.attrs.get("id"), str) and el.attrs["id"] in facts.label_for:
            el.label = facts.label_for[el.attrs["id"]]
        kept.append(el)
    facts.elements = kept

    # calls: api, timers, storage
    for node in _walk(rootn):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is None or args is None:
            continue
        fname = _text(fn)
        arglist = [a for a in args.children if a.type not in ("(", ")", ",")]
        line = node.start_point[0] + 1
        encl = _enclosing_function_name(node)
        if fname.endswith("request") and len(arglist) >= 3:
            method = _string_value(arglist[1]) if arglist[1].type == "string" else None
            path = _string_value(arglist[2]) if arglist[2].type in ("string", "template_string") else None
            if method and path:
                facts.api_calls.append(ApiCall(rel, encl, method.upper(), path, line))
        elif fname == "fetch" and arglist:
            path = _string_value(arglist[0]) if arglist[0].type in ("string", "template_string") else None
            method = "GET"
            if len(arglist) > 1:
                for s in _string_literals(arglist[1]):
                    if s.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        method = s.upper()
            if path:
                facts.api_calls.append(ApiCall(rel, encl, method, path, line))
        elif re.match(r"^(axios|api|http)\.(get|post|put|patch|delete)$", fname) and arglist:
            path = _string_value(arglist[0]) if arglist[0].type in ("string", "template_string") else None
            if path:
                facts.api_calls.append(ApiCall(rel, encl, fname.split(".")[1].upper(), path, line))
        elif fname in ("setTimeout", "setInterval") and len(arglist) >= 2:
            ms = None
            a = arglist[1]
            if a.type == "number":
                try:
                    ms = int(float(_text(a)))
                except ValueError:
                    ms = None
            elif a.type == "identifier" and isinstance(facts.const_values.get(_text(a)), int):
                ms = facts.const_values[_text(a)]
            elif a.type == "binary_expression":
                try:
                    ms = int(eval(_text(a), {"__builtins__": {}}, {k: v for k, v in facts.const_values.items() if isinstance(v, int)}))
                except Exception:
                    ms = None
            cb = arglist[0]
            cb_calls: list[str] = []
            if cb.type in ("arrow_function", "function_expression", "function"):
                cb_calls = _call_names(cb)
            elif cb.type == "identifier" and _text(cb) in facts.functions:
                cb_calls = _call_names(facts.functions[_text(cb)])
            facts.timers.append(TimerFact(rel, line, fname, ms, encl, sorted(set(cb_calls))[:12]))
        elif re.match(r"^(localStorage|sessionStorage)\.(getItem|setItem|removeItem)$", fname) and arglist:
            a = arglist[0]
            key = _string_value(a) if a.type == "string" else (str(facts.const_values.get(_text(a), _text(a))) if a.type == "identifier" else _text(a))
            facts.storage.append(StorageFact(rel, line, fname.split(".")[1], key))

    return facts


def _label_for(node: Node, attrs: dict, tag: str) -> tuple[str | None, list[str]]:
    alts: list[str] = []
    for key in ("aria-label", "label", "title", "placeholder", "alt"):
        if key in attrs:
            k, v = attrs[key]
            if k in ("str", "template") and str(v).strip():
                return str(v).strip(), alts
            if k == "alts" and v:
                return v[0], list(v)
    if tag == "input" and attrs.get("type", ("", ""))[1] in ("submit", "button") and "value" in attrs:
        return str(attrs["value"][1]), alts
    if node.type != "jsx_element":
        return None, alts
    texts: list[str] = []
    for c in node.children:
        if c.type == "jsx_text":
            t = re.sub(r"\s+", " ", _text(c)).strip()
            if t:
                texts.append(t)
        elif c.type == "jsx_expression":
            inner = c.children[1] if len(c.children) > 2 else None
            if inner is None:
                continue
            nested = [n for n in _walk(inner) if n.type in ("jsx_element", "jsx_self_closing_element")]
            if nested:
                tops = [n for n in nested if not _has_jsx_ancestor_within(n, inner)]
                for sub in tops[:3]:
                    sub_opening = sub.child_by_field_name("open_tag") if sub.type == "jsx_element" else sub
                    sub_attrs = _attr_map(sub_opening) if sub_opening is not None else {}
                    sub_label, _ = _label_for(sub, sub_attrs, _tag_name(sub_opening) if sub_opening is not None else "")
                    if sub_label and not sub_label.startswith("{"):
                        texts.append(sub_label)
                continue
            lits = _string_literals(c)
            if lits:
                if inner is not None and inner.type == "ternary_expression" and len(lits) >= 2:
                    alts.extend(lits)
                    texts.append(lits[-1])
                else:
                    texts.append(" ".join(lits))
            elif inner is not None and inner.type in ("identifier", "member_expression"):
                texts.append("{" + _text(inner) + "}")
        elif c.type in ("jsx_element", "jsx_self_closing_element"):
            sub_opening = c.child_by_field_name("open_tag") if c.type == "jsx_element" else c
            sub_attrs = _attr_map(sub_opening) if sub_opening is not None else {}
            sub_label, _ = _label_for(c, sub_attrs, _tag_name(sub_opening) if sub_opening is not None else "")
            if sub_label and not sub_label.startswith("{"):
                texts.append(sub_label)
    label = " ".join(texts).strip() or None
    if label and len(label) > 80:
        label = label[:77] + "..."
    return label, alts


def _has_jsx_ancestor_within(n: Node, limit: Node) -> bool:
    cur = n.parent
    while cur is not None and cur is not limit:
        if cur.type in ("jsx_element", "jsx_self_closing_element"):
            return True
        cur = cur.parent
    return False


def _handler_facts(attrs: dict, facts: FileFacts, event: str | None) -> tuple[list[str], list[str], list[str], list[str]]:
    navigates: list[str] = []
    opens: list[str] = []
    calls: list[str] = []
    deferred: list[str] = []
    if not event or event not in attrs:
        return navigates, opens, calls, deferred
    kind, value = attrs[event]
    body: Node | None = None
    if kind == "ident" and value in facts.functions:
        body = facts.functions[value]
    if kind == "ident" and body is None:
        calls.append(str(value))
    names: list[str] = []
    if body is not None:
        names = _call_names(body, shallow=True)
        deferred = [n for n in _call_names(body) if n not in names]
        navigates = _navigate_targets(body)
    elif kind == "inline":
        # the raw text of the arrow function; re-parse is overkill, scan names
        names = re.findall(r"([A-Za-z_][\w.]*)\s*\(", str(value))
        navigates = re.findall(r"navigate\(\s*['\"]([^'\"]+)['\"]", str(value))
    # one level into local helpers: onClick={() => { close(); resetPassword(); }}
    for nm in list(names):
        fn = facts.functions.get(nm)
        if fn is None or fn is body:
            continue
        inner = _call_names(fn, shallow=True)
        names.extend(n for n in inner if n not in names)
        deferred.extend(n for n in _call_names(fn) if n not in inner and n not in deferred)
        navigates.extend(_navigate_targets(fn))
    for name in names:
        short = name.split(".")[-1]
        if short in ("openDrawer", "openModal", "setDrawer", "showDrawer"):
            opens.append("drawer")
        elif short in ("openConfirm", "confirmDelete", "setConfirm", "showConfirm", "askConfirm") or name in ("confirm", "window.confirm"):
            opens.append("confirm")
        elif short.startswith("setOpen") or short in ("toggle", "toggleMenu", "setExpanded", "setShow"):
            opens.append("toggle")
        elif short == "logout" or short == "signOut":
            calls.append("logout")
        elif short in ("navigate", "setState", "preventDefault", "stopPropagation", "map", "filter", "find", "includes", "then", "catch", "finally", "join", "push", "trim"):
            continue
        elif short.startswith("set") and short[3:4].isupper():
            continue
        else:
            calls.append(name)
    # local helpers were expanded above; keep only names that can resolve to API functions
    calls = [c for c in calls if c not in facts.functions]
    deferred_api = sorted({d for d in deferred if d not in facts.functions and ("." in d or d.endswith("Api") or d[:1].islower() and d not in names)})[:12]
    return navigates, sorted(set(opens)), sorted(set(calls))[:12], deferred_api


def _root_jsx(fnode: Node) -> Node | None:
    """The JSX element a component's final return renders, descending through fragments."""
    body = fnode.child_by_field_name("body")
    scope = body if body is not None else fnode
    returns = [n for n in _walk_shallow(scope) if n.type == "return_statement"]
    if returns:
        scope = returns[-1]
    for node in _walk_shallow(scope):
        if node.type in ("jsx_element", "jsx_self_closing_element"):
            cur = node
            for _ in range(3):
                opening = cur.child_by_field_name("open_tag") if cur.type == "jsx_element" else cur
                if opening is not None and _tag_name(opening) == "" and cur.type == "jsx_element":
                    inner = [c for c in cur.children if c.type in ("jsx_element", "jsx_self_closing_element")]
                    if not inner:
                        return None
                    cur = inner[0]
                    continue
                return cur
            return cur
    return None


def _route_from(node: Node, opening: Node, attrs: dict, facts: FileFacts) -> CodeRoute | None:
    path = attrs.get("path", (None, None))[1] if "path" in attrs else None
    if path is None and "index" in attrs:
        cur = node.parent
        while cur is not None and path is None:
            if cur.type == "jsx_element":
                o = cur.child_by_field_name("open_tag")
                if o is not None and _tag_name(o) == "Route":
                    pa = _attr_map(o)
                    if "path" in pa:
                        path = pa["path"][1]
            cur = cur.parent
        path = path or "/"
    if path is None:
        return None
    element = None
    el = attrs.get("element")
    if el:
        m = re.search(r"<\s*([A-Za-z_][\w.]*)", str(el[1]))
        element = m.group(1) if m else None
    if element is None and "Component" in attrs:
        element = str(attrs["Component"][1])
    protected = False
    layouts: list[str] = []
    cur = node.parent
    while cur is not None:
        if cur.type == "jsx_element":
            o = cur.child_by_field_name("open_tag")
            if o is not None and _tag_name(o) == "Route":
                pa = _attr_map(o)
                pel = pa.get("element")
                name = None
                if pel:
                    m = re.search(r"<\s*([A-Za-z_][\w.]*)", str(pel[1]))
                    name = m.group(1) if m else None
                if name:
                    if GUARD_RE.search(name):
                        protected = True
                    else:
                        layouts.append(name)
        cur = cur.parent
    if element and GUARD_RE.search(element):
        protected = True
    return CodeRoute(path=str(path), element=element, protected=protected, layouts=layouts, file=facts.rel, line=node.start_point[0] + 1)


# ---------------------------------------------------------------------------
# whole-repo inventory
# ---------------------------------------------------------------------------

def _resolve_import(from_file: Path, spec: str, root: Path) -> str | None:
    if not spec.startswith("."):
        return None
    base = (from_file.parent / spec).resolve()
    candidates = [base] + [base.with_suffix(s) for s in (".jsx", ".js", ".tsx", ".ts")] + [base / f"index{s}" for s in (".jsx", ".js", ".tsx", ".ts")]
    for c in candidates:
        if c.is_file():
            try:
                return str(c.relative_to(root))
            except ValueError:
                return None
    return None


def build_inventory(repo_root: Path, framework: str = "react") -> CodeInventory:
    repo_root = Path(repo_root).resolve()
    inv = CodeInventory(framework=framework)
    files: dict[str, FileFacts] = {}
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if ".test." in path.name or ".spec." in path.name or path.name.endswith(".d.ts") or path.name.startswith("vite.config") or path.name.startswith("vitest."):
            continue
        try:
            facts = parse_file(path, repo_root)
        except Exception as exc:  # keep going; one bad file must not sink the run
            inv.warnings.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        files[facts.rel] = facts
        inv.files_parsed += 1

    # wrappers from any file: component name -> native kind (resolve chains like DropdownMenu -> Button -> button)
    wrapper_info: dict[str, dict] = {}
    for f in files.values():
        wrapper_info.update(f.wrapper_info)
        inv.wrappers.update(f.wrappers)
    inv.wrappers.setdefault("Link", "link")
    inv.wrappers.setdefault("NavLink", "link")
    for _ in range(4):
        changed = False
        for name, info in wrapper_info.items():
            if info.get("kind") is None and info.get("root_tag") in inv.wrappers:
                info["kind"] = inv.wrappers[info["root_tag"]]
                inv.wrappers[name] = info["kind"]
                changed = True
        if not changed:
            break

    # resolve instances of wrapper components; drop plain display components
    for f in files.values():
        resolved = []
        for el in f.elements:
            info = wrapper_info.get(el.tag)
            wkind = inv.wrappers.get(el.tag)
            if wkind and not el.attrs.get("role"):
                el.kind = wkind
                if info:
                    lp = info.get("label_prop")
                    root_label = info.get("root_label")
                    if lp and isinstance(el.attrs.get(lp), str):
                        label_value = el.attrs[lp]
                    elif lp and el.label and not root_label:
                        label_value = el.label
                    else:
                        label_value = info.get("default_label")
                    if root_label and "{" in root_label:
                        # the wrapper renders a template; fill it from the instance's props and the wrapper's defaults
                        values = {**info.get("defaults", {}), **{k: v for k, v in el.attrs.items() if isinstance(v, str) and v}}
                        if label_value:
                            values["label"] = label_value
                        el.label = _substitute(root_label, values)
                    elif root_label and not el.label:
                        el.label = root_label
                    elif lp:
                        el.label = label_value or el.label
                    elif not el.label and info.get("default_label"):
                        el.label = info["default_label"]
            if any(wrapper_info.get(a, {}).get("children_conditional") for a in el.ancestors):
                el.conditional = True
            if el.kind == "component":
                if el.event or el.href:
                    el.kind = "clickable"
                else:
                    continue
            if el.kind == "form":
                el.label = None
            resolved.append(el)
        f.elements = resolved

    # routes: pick the file(s) that declare them
    router_files = [f for f in files.values() if f.routes]
    for f in router_files:
        inv.routes.extend(f.routes)
        inv.api_calls.extend(f.api_calls)
    for f in files.values():
        if f not in router_files:
            inv.api_calls.extend(f.api_calls)
        inv.timers.extend(f.timers)
        inv.storage.extend(f.storage)
        inv.navigates.extend(f.navigates)

    # route -> page file, via the router file's imports
    page_file_for_route: dict[str, str] = {}
    layout_files: set[str] = set()
    for rf in router_files:
        rf_path = repo_root / rf.rel
        for r in rf.routes:
            if r.element and r.element in rf.imports:
                target = _resolve_import(rf_path, rf.imports[r.element], repo_root)
                if target:
                    page_file_for_route[r.path] = target
            for lay in r.layouts:
                if lay in rf.imports:
                    t = _resolve_import(rf_path, rf.imports[lay], repo_root)
                    if t:
                        layout_files.add(t)
    inv.page_files = page_file_for_route

    # transitive local JSX imports per page file (depth-limited)
    def local_imports(rel: str, depth: int = 0, seen: set | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        if depth > 3 or rel not in files:
            return seen
        for spec in files[rel].imports.values():
            t = _resolve_import(repo_root / rel, spec, repo_root)
            if t and t not in seen and t in files and t not in layout_files and t not in page_file_for_route.values():
                seen.add(t)
                local_imports(t, depth + 1, seen)
        return seen

    usage_count: dict[str, int] = {}
    page_deps: dict[str, set[str]] = {}
    for route, pf in page_file_for_route.items():
        deps = local_imports(pf)
        page_deps[route] = deps
        for d in deps:
            usage_count[d] = usage_count.get(d, 0) + 1

    # layout deps
    layout_deps: set[str] = set()
    for lf in layout_files:
        layout_deps |= local_imports(lf)

    protected_routes = [r.path for r in inv.routes if r.protected]

    key_counts: dict[str, int] = {}

    def add_element(src: CodeElement, route: str, shared: bool) -> None:
        el = CodeElement(**asdict(src))
        el.route = route
        el.shared = shared
        if shared:
            el.conditional = True
        base = f"{route}|{el.kind}|{(el.label or el.handler or el.href or el.tag)}|{el.file}:{el.line}"
        n = key_counts.get(base, 0)
        key_counts[base] = n + 1
        el.key = base if n == 0 else f"{base}#{n}"
        inv.elements.append(el)

    for route, pf in page_file_for_route.items():
        if pf in files:
            for el in files[pf].elements:
                add_element(el, route, shared=False)
        for d in page_deps.get(route, ()):
            if d in files:
                for el in files[d].elements:
                    add_element(el, route, shared=True)

    for lf in layout_files | layout_deps:
        if lf in files:
            for el in files[lf].elements:
                add_element(el, "(layout)", shared=False)

    # routes without a resolvable page file still count
    for r in inv.routes:
        if r.path not in page_file_for_route:
            inv.warnings.append(f"route {r.path}: page component {r.element} not resolved")

    inv.warnings.append(f"protected routes: {len(protected_routes)}; layout files: {sorted(layout_files)}")
    return inv
