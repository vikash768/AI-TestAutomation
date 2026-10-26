"""Browser-side write guard and fault injection.

While Phase 2 clicks Save buttons, opens destructive confirmations and submits
forms, every request that could change server state (anything but GET / HEAD /
OPTIONS) is answered in the browser with a blocked-by-client error and never
leaves the machine. The only writes let through are session plumbing (token
refresh), which a page needs just to stay signed in.

The same route handler carries fault injection: answer API reads with a 502,
drop them as if offline, or hold them to simulate a slow backend.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Request, Route

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_PLUMBING = re.compile(r"/(refresh|token)(?:[/?#]|$)", re.I)
API_TYPES = {"fetch", "xhr"}

# sendBeacon and websockets bypass ordinary request routing semantics; neither is
# needed to exercise a UI, so under the guard they are switched off.
BLOCK_INIT_SCRIPT = """
(() => {
  try { Object.defineProperty(Navigator.prototype, 'sendBeacon', { value: function () { return false; }, configurable: true }); } catch (e) {}
})();
"""


@dataclass
class Intercepted:
    method: str
    url: str
    path: str
    resource_type: str
    body_keys: list = field(default_factory=list)
    ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def body_keys(req: Request) -> list[str]:
    """Field names only. Values can hold passwords and are never recorded."""
    try:
        data = req.post_data_json
        if isinstance(data, dict):
            return sorted(str(k) for k in data.keys())[:30]
        if isinstance(data, list):
            return ["[list]"]
    except Exception:
        pass
    try:
        raw = req.post_data or ""
        if "=" in raw:
            return sorted(parse_qs(raw).keys())[:30]
    except Exception:
        pass
    return []


def is_api_read(req: Request) -> bool:
    return req.resource_type in API_TYPES and req.method.upper() == "GET" and not SESSION_PLUMBING.search(urlparse(req.url).path)


class NetworkGuard:
    def __init__(self, context: BrowserContext, allow_non_get: re.Pattern = SESSION_PLUMBING):
        self.context = context
        self.allow = allow_non_get
        self.intercepted: list[Intercepted] = []
        self.injection: Callable[[Route, Request], bool] | None = None
        self.held: list[Route] = []
        self.injected: list[dict] = []
        self._suspended = 0
        self._installed = False

    # -- lifecycle ---------------------------------------------------------
    def install(self) -> "NetworkGuard":
        self.context.add_init_script(BLOCK_INIT_SCRIPT)
        self.context.route("**/*", self._handle)
        with contextlib.suppress(Exception):
            self.context.route_web_socket(re.compile(".*"), lambda ws: ws.close())
        self._installed = True
        return self

    @contextlib.contextmanager
    def suspended(self):
        """Let everything through (used for sign-in and for the real logout test)."""
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1

    # -- routing -----------------------------------------------------------
    def _handle(self, route: Route) -> None:
        req = route.request
        method = req.method.upper()
        path = urlparse(req.url).path
        if not self._suspended and method not in READ_METHODS and not self.allow.search(path):
            self.intercepted.append(Intercepted(method, req.url[:300], path, req.resource_type, body_keys(req), time.time()))
            with contextlib.suppress(Exception):
                route.abort("blockedbyclient")
            return
        inj = self.injection
        if inj is not None and not self._suspended:
            try:
                if inj(route, req):
                    return
            except Exception:
                pass
        with contextlib.suppress(Exception):
            route.fallback()

    def mark(self) -> int:
        return len(self.intercepted)

    def since(self, mark: int) -> list[Intercepted]:
        return self.intercepted[mark:]

    # -- fault injection ---------------------------------------------------
    def inject_server_error(self, status: int = 502) -> None:
        body = f"<html><head><title>{status} Bad Gateway</title></head><body><h1>{status} Bad Gateway</h1></body></html>"

        def handler(route: Route, req: Request) -> bool:
            if not is_api_read(req):
                return False
            self.injected.append({"mode": f"http{status}", "url": req.url[:300]})
            route.fulfill(status=status, content_type="text/html", body=body)
            return True

        self.injection = handler

    def inject_offline(self) -> None:
        def handler(route: Route, req: Request) -> bool:
            if not is_api_read(req):
                return False
            self.injected.append({"mode": "offline", "url": req.url[:300]})
            route.abort("internetdisconnected")
            return True

        self.injection = handler

    def inject_hold(self) -> None:
        """Hold API reads unanswered until release_held(): a backend that is slow to respond."""
        def handler(route: Route, req: Request) -> bool:
            if not is_api_read(req):
                return False
            self.injected.append({"mode": "hold", "url": req.url[:300]})
            self.held.append(route)
            return True

        self.injection = handler

    def release_held(self) -> None:
        held, self.held = self.held, []
        for r in held:
            with contextlib.suppress(Exception):
                r.fallback()

    def clear_injection(self) -> None:
        self.injection = None
        self.release_held()


def describe(writes: list[Intercepted], limit: int = 3) -> str:
    return ", ".join(f"{w.method} {w.path}" for w in writes[:limit]) + (f" (+{len(writes) - limit} more)" if len(writes) > limit else "")


def path_regex(code_path: str) -> re.Pattern:
    """'/api/orgs/${orgId}/members${toQueryString(f)}' -> matches '/api/orgs/12/members'."""
    p = code_path.split("?")[0]
    tail = re.search(r"\$\{[^}]*\}$", p)
    if tail and not p[: tail.start()].endswith("/"):
        p = p[: tail.start()]  # members${toQueryString(f)}: a query suffix, not a path segment
    parts = re.split(r"\$\{[^}]*\}", p)
    rx = "[^/]+".join(re.escape(x) for x in parts)
    return re.compile("(?:^|/)" + rx.lstrip("/") + r"/?$")


def matches_any(path: str, code_paths: list[str]) -> str | None:
    for cp in code_paths:
        if path_regex(cp).search(path):
            return cp
    return None


def to_json(writes: list[Intercepted]) -> list[dict]:
    return [w.to_dict() for w in writes]


__all__ = ["NetworkGuard", "Intercepted", "describe", "matches_any", "path_regex", "is_api_read", "to_json", "json"]
