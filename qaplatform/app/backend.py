"""What a native app's backend offers, and which of it the app calls.

Read from the optional second codebase (the backend) and from the app's own
code. Kept apart from the UI inventory: backend files never count towards
component coverage. Used to plan journeys (which screen action reaches which
endpoint) and to pick load-test targets.

  backend_routes(repo)     METHOD /path declared by Express / Koa / Fastify / Hono / NestJS / FastAPI / Flask / Django,
                           with mount prefixes (app.use('/api', router)) and whether a sign-in guard covers them
  client_endpoints(repo)   METHOD /path the app requests (axios / fetch / request(base, '/path', {method}))
  base_urls(repo)          deployed API origins written in the app's code or config (never localhost)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "coverage", "__pycache__", ".venv", "venv", "android", "ios", "Pods", "vendor", "migrations", "__tests__", "test", "tests", "e2e"}
METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "all")

EXPRESS_RE = re.compile(r"\b(\w+)\s*\.\s*(get|post|put|patch|delete|head|options|all)\s*\(\s*['\"`](/[^'\"`]*)['\"`]([^\n]{0,240})", re.I)
MOUNT_RE = re.compile(r"\.\s*(?:use|route|register)\s*\(\s*['\"`](/[^'\"`]*)['\"`]\s*,\s*(?:[\w.]+\s*\([^)]*\)\s*,\s*|[\w.]+\s*,\s*)*([A-Za-z_$][\w$]*)\s*[,)]")
FASTIFY_PREFIX_RE = re.compile(r"\.register\s*\(\s*([A-Za-z_$][\w$]*)\s*,\s*\{[^}]*prefix\s*:\s*['\"`](/[^'\"`]*)['\"`]")
IMPORT_RE = re.compile(r"import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)[^'\"]*?from\s+['\"](\.[^'\"]+)['\"]|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['\"](\.[^'\"]+)['\"]\s*\)")
NEST_CTRL_RE = re.compile(r"@Controller\s*\(\s*(?:['\"`]([^'\"`]*)['\"`])?")
NEST_ROUTE_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Head|Options|All)\s*\(\s*(?:['\"`]([^'\"`]*)['\"`])?\s*\)")
PY_ROUTE_RE = re.compile(r"@\s*\w+\s*\.\s*(get|post|put|patch|delete|head|options|route|api_route)\s*\(\s*['\"]([^'\"]*)['\"]([^\n]{0,240})")
PY_PREFIX_RE = re.compile(r"(?:APIRouter|Blueprint)\s*\([^)]*?(?:url_)?prefix\s*=\s*['\"]([^'\"]+)['\"]")
DJANGO_RE = re.compile(r"\b(?:re_)?path\s*\(\s*r?['\"]([^'\"]*)['\"]\s*,\s*([\w.]+)")
AUTH_RE = re.compile(r"\b(auth\w*|requireAuth|requireUser|protect\w*|verify\w*Token|isAuthenticated|ensureAuth\w*|passport\.authenticate|jwt\w*|bearer|login_required|permission_classes|UseGuards|get_current_\w+|current_user|requireRole|requireOperator)\b", re.I)
PUBLIC_RE = re.compile(r"\b(public|no_?auth|allowAnonymous|AllowAny|Public\(\))\b")
ROUTER_USE_AUTH_RE = re.compile(r"\b\w+\s*\.\s*use\s*\(\s*(?!['\"`])[^)]*" + AUTH_RE.pattern, re.I)

API_RE = re.compile(r"\b(?:axios|api|http|client|request|instance|\w+Api|\w+Client)\s*\.\s*(get|post|put|patch|delete|head)\s*(?:<[^>()]*>)?\s*\(\s*['\"`](/[^'\"`]*)['\"`]", re.I)
FETCH_RE = re.compile(r"\bfetch\s*\(\s*['\"`]([^'\"`]+)['\"`]\s*(?:,\s*\{([^}]{0,240})\})?", re.I)
CALL_RE = re.compile(r"\b(\w*(?:request|fetch|call|api|http)\w*)\s*(?:<[^>()]*(?:<[^>()]*>[^>()]*)*>)?\s*\(\s*[\w.]+\s*,\s*['\"`](/[^'\"`\s]*)['\"`]\s*(?:,\s*\{([^}]{0,240})\})?", re.I)
WRAPPER_PREFIX_RE = re.compile(r"fetch\s*\(\s*`\$\{\s*\w+\s*\}(/[\w/-]+)\$\{\s*\w*path\w*\s*\}", re.I)
METHOD_RE = re.compile(r"method\s*:\s*['\"](\w+)['\"]", re.I)
BASE_URL_RE = re.compile(r"\b(\w*(?:BASE_?URL|API_?URL|SERVER_?URL|API_?BASE|BACKEND_?URL|baseURL|baseUrl|apiUrl|apiBase|serverUrl|ENDPOINT|HOST)\w*)\s*[:=]\s*['\"`](https?://[^'\"`\s/]+(?:/[^'\"`\s]*)?)['\"`]", re.I)
LOCAL_HOST_RE = re.compile(r"https?://(localhost|127\.|0\.0\.0\.0|10\.0\.2\.2|192\.168\.|example\.(com|org)|your[-.])", re.I)


@dataclass
class BackendRoute:
    method: str
    path: str
    file: str
    line: int
    auth: bool | None = None  # True: a sign-in guard covers it; False: declared public; None: unknown
    framework: str = ""


@dataclass
class ClientEndpoint:
    method: str
    path: str
    file: str
    line: int
    dynamic: bool = False  # the path needs an id or other value only known at run time


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix in SOURCE_SUFFIXES and not p.is_symlink() and p.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            if p.stat().st_size < 600_000 and not p.name.endswith((".min.js", ".d.ts", ".test.ts", ".test.js", ".spec.ts", ".spec.js")):
                out.append(p)
    return out


def _join(*parts: str) -> str:
    joined = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + joined if joined else "/"


def _resolve(from_file: Path, spec: str, root: Path) -> str | None:
    base = (from_file.parent / spec).resolve()
    for cand in (base, *(base.with_suffix(s) for s in (".ts", ".js", ".mjs", ".cjs", ".tsx")), *(base / f"index{s}" for s in (".ts", ".js"))):
        if cand.is_file():
            try:
                return str(cand.relative_to(root))
            except ValueError:
                return None
    return None


def _line(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def backend_routes(repo_root: Path) -> list[BackendRoute]:
    root = Path(repo_root).resolve()
    files = {str(p.relative_to(root)): p for p in _files(root)}
    sources = {rel: p.read_text(encoding="utf-8", errors="replace") for rel, p in files.items()}
    # mount prefixes: app.use('/api/results', resultsRouter) where resultsRouter comes from ./routes/results
    mounts: dict[str, list[tuple[str, str]]] = {}  # child file -> [(parent file, prefix)]
    for rel, src in sources.items():
        if not rel.endswith((".js", ".ts", ".mjs", ".cjs")):
            continue
        names = {}
        for m in IMPORT_RE.finditer(src):
            name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            target = _resolve(files[rel], spec, root)
            if target:
                names[name] = target
        for m in MOUNT_RE.finditer(src):
            if m.group(2) in names:
                mounts.setdefault(names[m.group(2)], []).append((rel, m.group(1)))
        for m in FASTIFY_PREFIX_RE.finditer(src):
            if m.group(1) in names:
                mounts.setdefault(names[m.group(1)], []).append((rel, m.group(2)))

    def prefix(rel: str, depth: int = 0) -> str:
        if depth > 5 or rel not in mounts:
            return ""
        parent, pre = mounts[rel][0]
        return _join(prefix(parent, depth + 1), pre)

    out: list[BackendRoute] = []
    seen: set[tuple[str, str]] = set()

    def add(method: str, path: str, rel: str, pos: int, auth: bool | None, fw: str) -> None:
        key = (method, path)
        if key not in seen:
            seen.add(key)
            out.append(BackendRoute(method=method, path=path, file=rel, line=_line(sources[rel], pos), auth=auth, framework=fw))

    for rel, src in sources.items():
        if rel.endswith(".py"):
            file_prefix = (PY_PREFIX_RE.search(src).group(1) if PY_PREFIX_RE.search(src) else "")
            for m in PY_ROUTE_RE.finditer(src):
                verb = m.group(1).lower()
                rest = m.group(3)
                methods = re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE)['\"]", rest, re.I) if verb in ("route", "api_route") else [verb]
                window = src[m.start(): m.start() + 600]
                auth = True if AUTH_RE.search(window) else (False if PUBLIC_RE.search(window) else None)
                for meth in methods or ["get"]:
                    add(meth.upper(), _join(file_prefix, m.group(2)), rel, m.start(), auth, "python")
            if rel.endswith("urls.py"):
                for m in DJANGO_RE.finditer(src):
                    add("ANY", _join(m.group(1)), rel, m.start(), None, "django")
            continue
        guarded_from = min((m.start() for m in ROUTER_USE_AUTH_RE.finditer(src)), default=None)
        file_prefix = prefix(rel)
        for m in EXPRESS_RE.finditer(src):
            obj, verb, path, rest = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
            if obj in ("axios", "api", "http", "client", "request", "instance", "fetch", "window", "this", "map", "params", "query", "headers", "cache", "storage", "AsyncStorage"):
                continue  # a client call or a map lookup, not a route
            if verb == "get" and not re.search(r"[,(]\s*(async\s*)?(\(|function|\w+\s*=>|[\w.]+\s*[,)])", rest):
                continue  # app.get('setting') reads a setting
            auth = True if AUTH_RE.search(rest) or (guarded_from is not None and m.start() > guarded_from) else (False if PUBLIC_RE.search(rest) else None)
            add("ANY" if verb == "all" else verb.upper(), _join(file_prefix, path), rel, m.start(), auth, "node")
        ctrl = NEST_CTRL_RE.search(src)
        if ctrl:
            guarded = "UseGuards" in src[: ctrl.start()] or None
            for m in NEST_ROUTE_RE.finditer(src):
                window = src[max(0, m.start() - 200): m.start()]
                auth = True if guarded or "UseGuards" in window else (False if PUBLIC_RE.search(window) else None)
                add(m.group(1).upper(), _join(file_prefix, ctrl.group(1) or "", m.group(2) or ""), rel, m.start(), auth, "nestjs")
    return out


def client_endpoints(repo_root: Path) -> list[ClientEndpoint]:
    root = Path(repo_root).resolve()
    out: list[ClientEndpoint] = []
    seen: set[tuple[str, str]] = set()
    for p in _files(root):
        if p.suffix == ".py":
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        rel = str(p.relative_to(root))
        hits = []
        for m in API_RE.finditer(src):
            hits.append((m.group(1).upper(), m.group(2), m.start()))
        for m in FETCH_RE.finditer(src):
            url = m.group(1)
            if url.startswith("/") or url.startswith("http"):
                meth = METHOD_RE.search(m.group(2) or "")
                hits.append(((meth.group(1) if meth else "GET").upper(), url, m.start()))
        for m in CALL_RE.finditer(src):
            meth = METHOD_RE.search(m.group(3) or "")
            hits.append(((meth.group(1) if meth else "GET").upper(), m.group(2), m.start()))
        # a wrapper that builds the URL as `${base}/api${path}` adds that prefix to every path it is given
        wrap = WRAPPER_PREFIX_RE.search(src)
        for method, path, pos in hits:
            path = path.split("?", 1)[0]
            dynamic = bool(re.search(r"/\$\{|/:\w", path))  # /results/${id}, /results/:id
            clean = path if dynamic else re.sub(r"\$\{[^}]*\}$", "", path)  # trailing ${query(params)}
            if wrap and clean.startswith("/") and not clean.startswith(wrap.group(1)):
                clean = wrap.group(1).rstrip("/") + clean
            key = (method, clean)
            if clean and key not in seen:
                seen.add(key)
                out.append(ClientEndpoint(method=method, path=clean, file=rel, line=_line(src, pos), dynamic=dynamic))
    return out


def base_urls(repo_root: Path) -> list[str]:
    """Deployed API origins the app's code or config names; localhost and placeholders are ignored."""
    root = Path(repo_root).resolve()
    found: list[str] = []
    candidates = _files(root) + [p for p in (root / "app.json", root / ".env.example", root / ".env.production") if p.is_file()]
    for p in candidates:
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in BASE_URL_RE.finditer(src):
            url = m.group(2).rstrip("/")
            if not LOCAL_HOST_RE.match(url) and url not in found:
                found.append(url)
    return found


def summarize(routes: list[BackendRoute], endpoints: list[ClientEndpoint]) -> dict:
    return {"routes": [asdict(r) for r in routes], "client_endpoints": [asdict(e) for e in endpoints]}
