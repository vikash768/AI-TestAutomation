"""Crawl the live application and record what is actually there.

Two passes: unauthenticated (do protected routes send us to login?) and
authenticated (breadth-first over code routes plus discovered links). Every
page is captured at five viewports with a layout check and a screenshot.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from qaplatform.config import Settings
from qaplatform.explore.browser import BrowserSession, LoginResult, attach_monitor, goto, login, same_origin
from qaplatform.explore.scripts import COLLECT_INTERACTIVE_JS, LAYOUT_CHECK_JS, PAGE_STATE_JS
from qaplatform.ingest.js_parser import CodeRoute

logger = logging.getLogger(__name__)

EventCb = Callable[[str, str, int | None], None]


@dataclass
class ObservedElement:
    selector: str
    role: str
    name: str
    tag: str
    type: str | None = None
    href: str | None = None
    id: str | None = None
    testid: str | None = None
    visible: bool = True
    enabled: bool = True
    in_dialog: bool = False
    checked: bool | None = None
    bbox: list = field(default_factory=list)
    path_hint: str = ""
    target: str | None = None
    download: bool = False
    haspopup: str | None = None
    expanded: str | None = None


@dataclass
class ObservedPage:
    route: str
    url: str
    final_url: str = ""
    title: str = ""
    status_code: int | None = None
    load_ms: int = 0
    protected: bool | None = None
    elements: list[ObservedElement] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    viewports: dict = field(default_factory=dict)
    screenshot: str = ""
    state: dict = field(default_factory=dict)
    api_requests: list[dict] = field(default_factory=list)
    revealed: bool = False
    opener_file: str | None = None
    reveal: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class GuardResult:
    route: str
    url: str
    final_url: str
    redirected_to_login: bool
    shows_login_form: bool
    protected_in_code: bool


@dataclass
class ExploreResult:
    pages: list[ObservedPage] = field(default_factory=list)
    guards: list[GuardResult] = field(default_factory=list)
    login: LoginResult | None = None
    storage_state_path: str = ""
    warnings: list[str] = field(default_factory=list)
    origin: str = ""


def _slug(path: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    return s or "root"


def _norm_path(url: str) -> str:
    p = urlparse(url).path or "/"
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _route_pattern(path: str) -> re.Pattern:
    esc = re.escape(path)
    esc = re.sub(r"\\:[A-Za-z_]+", r"[^/]+", esc)
    esc = esc.replace(r"\*", ".*")
    return re.compile("^" + esc + "$")


def route_for(path: str, routes: list[CodeRoute]) -> str | None:
    for r in routes:
        if r.path == path:
            return r.path
    for r in routes:
        if ":" in r.path or "*" in r.path:
            if _route_pattern(r.path).match(path):
                return r.path
    return None


def _capture_page(page, url: str, route: str, settings: Settings, shots_dir: Path, mon, protected: bool | None) -> ObservedPage:
    mon.reset()
    t0 = time.time()
    resp = goto(page, url, settings)
    load_ms = int((time.time() - t0) * 1000)
    state = page.evaluate(PAGE_STATE_JS)
    elements_raw = page.evaluate(COLLECT_INTERACTIVE_JS, settings.max_elements_per_page)
    elements = [ObservedElement(**{k: v for k, v in e.items() if k in ObservedElement.__dataclass_fields__}) for e in elements_raw]
    shot = shots_dir / f"{_slug(route)}_desktop.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception:
        shot = Path("")
    snap = mon.snapshot()
    observed = ObservedPage(
        route=route,
        url=url,
        final_url=page.url,
        title=state.get("title", ""),
        status_code=resp.status if resp else None,
        load_ms=load_ms,
        protected=protected,
        elements=elements,
        console_errors=snap["console_errors"],
        page_errors=snap["page_errors"],
        failed_requests=snap["failed_requests"],
        screenshot=str(shot) if shot else "",
        state=state,
        api_requests=snap["api_requests"][:100],
    )
    # responsive pass
    for name, w, h in settings.viewports:
        try:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(350)
            layout = page.evaluate(LAYOUT_CHECK_JS, {"checkTargets": w <= 768})
            vshot = shots_dir / f"{_slug(route)}_{name}.png"
            page.screenshot(path=str(vshot), full_page=False)
            layout["screenshot"] = str(vshot)
            layout["width"] = w
            layout["height"] = h
            observed.viewports[name] = layout
        except Exception as exc:
            observed.viewports[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}", "width": w, "height": h}
    page.set_viewport_size({"width": 1440, "height": 900})
    return observed


def explore(settings: Settings, live_url: str, routes: list[CodeRoute], email: str, password: str, run_dir: Path, events: EventCb) -> ExploreResult:
    result = ExploreResult(origin=f"{urlparse(live_url).scheme}://{urlparse(live_url).netloc}")
    shots_dir = run_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    base = live_url.rstrip("/")
    protected_routes = [r for r in routes if r.protected and ":" not in r.path and "*" not in r.path]
    public_routes = [r for r in routes if not r.protected and ":" not in r.path and "*" not in r.path]

    with BrowserSession(settings) as session:
        # pass 1: unauthenticated guard checks
        ctx = session.new_context()
        page = ctx.new_page()
        mon = attach_monitor(page)
        events("explore", f"Checking {len(protected_routes)} protected routes without a session", 22)
        for r in protected_routes:
            url = base + r.path
            try:
                goto(page, url, settings)
                state = page.evaluate(PAGE_STATE_JS)
                final_path = _norm_path(page.url)
                result.guards.append(GuardResult(
                    route=r.path, url=url, final_url=page.url,
                    redirected_to_login="/login" in final_path or "/signin" in final_path,
                    shows_login_form=bool(state.get("hasPasswordField")),
                    protected_in_code=True,
                ))
            except Exception as exc:
                result.warnings.append(f"guard check {r.path}: {type(exc).__name__}: {str(exc)[:120]}")
        # public pages, unauthenticated
        for r in public_routes:
            url = base + r.path
            try:
                observed = _capture_page(page, url, r.path, settings, shots_dir, mon, protected=False)
                result.pages.append(observed)
            except Exception as exc:
                result.warnings.append(f"public page {r.path}: {type(exc).__name__}: {str(exc)[:120]}")
        ctx.close()

        # pass 2: login
        events("explore", "Logging in with the test account", 30)
        ctx = session.new_context()
        result.login = login(ctx, base, email, password, settings, run_dir)
        if not result.login.success:
            result.warnings.append(result.login.error)
            ctx.close()
            return result
        result.storage_state_path = result.login.storage_state_path
        page = ctx.new_page()
        mon = attach_monitor(page)

        queue: list[str] = []
        seen_paths: set[str] = set(_norm_path(p.url) for p in result.pages)
        for r in protected_routes:
            queue.append(base + r.path)
        queue.append(base + "/")
        visited: set[str] = set()
        count = 0
        while queue and len(result.pages) < settings.max_pages:
            url = queue.pop(0)
            path = _norm_path(url)
            if path in visited or path in seen_paths:
                continue
            visited.add(path)
            route = route_for(path, routes) or path
            count += 1
            events("explore", f"Exploring {path} ({len(result.pages) + 1}/{min(settings.max_pages, len(queue) + len(result.pages) + 1)})", min(30 + count * 2, 55))
            try:
                observed = _capture_page(page, url, route, settings, shots_dir, mon, protected=route_for(path, routes) is not None and any(r.path == route and r.protected for r in routes))
            except Exception as exc:
                result.warnings.append(f"page {path}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if _norm_path(observed.final_url) in ("/login", "/signin") and path not in ("/login", "/signin"):
                result.warnings.append(f"{path} redirected to login while authenticated")
            final = _norm_path(observed.final_url)
            if final != path and (final in visited or final in seen_paths or any(_norm_path(p.final_url) == final for p in result.pages)):
                continue  # a redirect to a page that is (or will be) captured under its own route
            result.pages.append(observed)
            for el in observed.elements:
                if el.href and same_origin(el.href, base) and not el.download:
                    p = _norm_path(el.href)
                    if p not in visited and p not in seen_paths and not re.search(r"logout|signout|\.(png|jpg|pdf|zip|dmg|exe)$", p, re.I):
                        queue.append(urljoin(base + "/", p.lstrip("/")))
        ctx.close()
    return result
