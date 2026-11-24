"""Playwright session, page monitors and the generic login flow."""
from __future__ import annotations

import logging
import os
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PWTimeout, sync_playwright

from qaplatform.config import Settings

logger = logging.getLogger(__name__)

NOISE_PATTERNS = (
    re.compile(r"favicon", re.I),
    re.compile(r"net::ERR_ABORTED", re.I),
    re.compile(r"ERR_BLOCKED_BY_CLIENT", re.I),  # writes stopped by the platform's own guard
)

# Called with every new BrowserContext (tests use it to serve a fixture app).
CONTEXT_HOOKS: list = []


@dataclass
class Monitor:
    """Collects console errors, page errors and failed requests for one page."""

    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict] = field(default_factory=list)
    api_requests: list[dict] = field(default_factory=list)
    dialogs: list[dict] = field(default_factory=list)
    crashed: bool = False
    requests_seen: int = 0
    # The page's own origin host, auto-detected from its main document. Errors and failed requests
    # from OTHER hosts (Analytics, ads, fonts, chat widgets, Sentry) are the third party's, not the
    # site's — reported as information, never as the site's bug.
    origin: str = ""
    third_party_console: list = field(default_factory=list)
    third_party_failed: list = field(default_factory=list)
    # Native confirm() is dismissed by default (nothing is ever confirmed by accident). The
    # provision module sets this only around the Delete click on its own demo record.
    accept_confirm: bool = False

    def reset(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.failed_requests.clear()
        self.api_requests.clear()
        self.dialogs.clear()
        self.third_party_console.clear()
        self.third_party_failed.clear()
        self.crashed = False
        self.requests_seen = 0

    def first_party(self, url: str) -> bool:
        if not self.origin:
            return True  # origin not detected yet: be conservative, treat as the site's own
        h = urlparse(url).hostname or ""
        h = h[4:] if h.startswith("www.") else h
        o = self.origin[4:] if self.origin.startswith("www.") else self.origin
        return not h or h == o or h.endswith("." + o) or o.endswith("." + h)

    def snapshot(self) -> dict:
        return {
            "console_errors": list(dict.fromkeys(self.console_errors)),
            "page_errors": list(dict.fromkeys(self.page_errors)),
            "failed_requests": list(self.failed_requests),
            "third_party_console": list(dict.fromkeys(self.third_party_console)),
            "third_party_failed": list(self.third_party_failed),
            "api_requests": list(self.api_requests),
            "dialogs": list(self.dialogs),
            "crashed": self.crashed,
            "requests_seen": self.requests_seen,
        }


def attach_monitor(page: Page, origin: str = "") -> Monitor:
    mon = Monitor()
    if origin:
        from urllib.parse import urlparse as _up
        mon.origin = (_up(origin).hostname or origin) if "//" in origin else origin

    def on_console(msg):
        if msg.type in ("error",):
            text = msg.text[:300]
            if any(p.search(text) for p in NOISE_PATTERNS):
                return
            src = ""
            try:
                src = (msg.location or {}).get("url", "")
            except Exception:
                src = ""
            (mon.console_errors if (not src or mon.first_party(src)) else mon.third_party_console).append(text)

    def on_page_error(err):
        mon.page_errors.append(str(err)[:300])

    def on_request(req):
        mon.requests_seen += 1
        if req.resource_type in ("xhr", "fetch") and len(mon.api_requests) < 300:
            mon.api_requests.append({"method": req.method, "url": req.url[:300], "path": urlparse(req.url).path})

    def on_request_failed(req):
        if req.resource_type in ("document", "xhr", "fetch", "script", "stylesheet", "image"):
            text = req.failure or "failed"
            if "BLOCKED_BY_CLIENT" in text:
                return
            if not any(p.search(req.url) or p.search(text) for p in NOISE_PATTERNS):
                item = {"url": req.url[:300], "method": req.method, "status": None, "error": text[:120], "type": req.resource_type}
                (mon.failed_requests if mon.first_party(req.url) else mon.third_party_failed).append(item)

    def on_response(resp):
        try:
            rt = resp.request.resource_type
            if rt == "document" and not mon.origin and (resp.frame.parent_frame is None):
                mon.origin = urlparse(resp.url).hostname or ""  # the page's own origin, for first-party tagging
            if resp.status >= 400 and rt in ("document", "xhr", "fetch", "script", "stylesheet", "image"):
                if not any(p.search(resp.url) for p in NOISE_PATTERNS):
                    item = {"url": resp.url[:300], "method": resp.request.method, "status": resp.status, "error": "", "type": rt}
                    (mon.failed_requests if mon.first_party(resp.url) else mon.third_party_failed).append(item)
        except Exception:
            pass

    def on_dialog(dialog):
        mon.dialogs.append({"type": dialog.type, "message": dialog.message[:200]})
        try:
            if mon.accept_confirm and dialog.type == "confirm":
                mon.accept_confirm = False  # one confirmation only
                dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass

    def on_crash(_page):
        mon.crashed = True

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("dialog", on_dialog)
    page.on("crash", on_crash)
    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    return mon


@dataclass
class LoginResult:
    success: bool
    final_url: str = ""
    error: str = ""
    storage_state_path: str = ""
    login_url: str = ""
    rate_limited: bool = False
    retry_after: int = 0
    server_error: int = 0


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Playwright | None = None
        self.browser: Browser | None = None

    def __enter__(self) -> "BrowserSession":
        # Playwright would connect to a Selenium Grid if this legacy variable is set.
        os.environ.pop("SELENIUM_REMOTE_URL", None)
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.settings.headless)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def new_context(self, width: int = 1440, height: int = 900, storage_state: str | None = None) -> BrowserContext:
        assert self.browser is not None
        kwargs = {"viewport": {"width": width, "height": height}, "ignore_https_errors": False, "service_workers": "block"}
        if storage_state:
            kwargs["storage_state"] = storage_state
        ctx = self.browser.new_context(**kwargs)
        ctx.set_default_timeout(self.settings.action_timeout_ms)
        ctx.set_default_navigation_timeout(self.settings.nav_timeout_ms)
        for hook in CONTEXT_HOOKS:
            hook(ctx)
        return ctx


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def goto(page: Page, url: str, settings: Settings):
    """Navigate and let the SPA settle. Returns the Response or None."""
    resp = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded")
    except PWTimeout:
        logger.warning("navigation timeout: %s", url)
    try:
        page.wait_for_load_state("networkidle", timeout=min(8000, settings.nav_timeout_ms))
    except PWTimeout:
        pass
    page.wait_for_timeout(settings.settle_ms)
    return resp


AUTH_TOKENS_JS = """() => { try {
  // Only values that hold a real credential: a JWT anywhere in the value (also inside a persisted
  // JSON store), or a long opaque string. A store that merely records {"token":null,"error":...}
  // or an attempt counter is not a session.
  const JWT = /[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}/;
  const OPAQUE = /^(Bearer )?[A-Za-z0-9_\\-+=\\/.]{24,}$/;
  return Object.keys(localStorage).filter(k => /token|session|auth|jwt/i.test(k)).map(k => {
    const v = localStorage.getItem(k) || '';
    const m = v.match(JWT);
    return m ? k + '=' + m[0] : (OPAQUE.test(v) ? k + '=' + v : null);
  }).filter(Boolean);
} catch (e) { return []; } }"""


def _auth_tokens(page) -> set:
    try:
        return set(page.evaluate(AUTH_TOKENS_JS))
    except Exception:
        return set()


def login(context: BrowserContext, live_url: str, email: str, password: str, settings: Settings, run_dir: Path, login_path: str = "/login", save_state: bool = True) -> LoginResult:
    page = context.new_page()
    page.set_default_timeout(settings.action_timeout_ms)
    login_url = urljoin(live_url.rstrip("/") + "/", login_path.lstrip("/"))
    goto(page, login_url, settings)

    email_sel = "input[type=email], input[name*=email i], input[id*=email i], input[autocomplete=username], input[name*=user i], input[id*=user i]"
    pass_sel = "input[type=password]"
    try:
        if page.locator(pass_sel).count() == 0:
            # maybe a landing page with a sign-in link
            link = page.get_by_role("link", name=re.compile(r"sign ?in|log ?in|login", re.I)).first
            if link.count():
                link.click()
                page.wait_for_timeout(settings.settle_ms)
        email_box = page.locator(email_sel).first
        pass_box = page.locator(pass_sel).first
        email_box.wait_for(state="visible", timeout=settings.nav_timeout_ms)
        rate_limited = {"hit": False, "retry_after": 0, "answered": False, "server_error": 0}

        def _on_resp(r):
            if r.request.method != "GET" and re.search(r"login|signin|sign-in|auth|session|token", r.url, re.I):
                rate_limited["answered"] = True  # the sign-in request has come back (accepted or not)
                if r.status >= 500:
                    rate_limited["server_error"] = r.status
            if r.status == 429 and re.search(r"login|signin|auth", r.url, re.I):
                rate_limited["hit"] = True
                with contextlib.suppress(Exception):
                    rate_limited["retry_after"] = int(r.headers.get("retry-after") or 0)
        page.on("response", _on_resp)
        start_path = urlparse(page.url).path
        tokens_before = _auth_tokens(page)
        email_box.fill(email)
        pass_box.fill(password)
        submit = page.locator("button[type=submit], input[type=submit]").first
        if submit.count():
            submit.click()
        else:
            btn = page.get_by_role("button", name=re.compile(r"sign ?in|log ?in|login|continue", re.I)).first
            if btn.count():
                btn.click()
            else:
                pass_box.press("Enter")
        # Success shows up in one of several ways. A classic app changes the URL away
        # from /login; a router-less SPA keeps the URL but drops the password field and
        # usually stores an auth token. Treat any of these as signed in.
        def _signed_in() -> bool:
            # Left the sign-in page (when it has its own path), a NEW auth token, or the
            # password field gone and staying gone. A sign-in screen served at "/" keeps the
            # path, so the path alone never counts there.
            path = urlparse(page.url).path
            if (path.rstrip("/") or "/") != (start_path.rstrip("/") or "/") and not re.search(r"/(login|signin|sign-in)(/|$)", path, re.I):
                return True
            if _auth_tokens(page) - tokens_before:
                return True
            # The password field going away counts only once the sign-in request has answered (a
            # spinner can hide the form while it is in flight) and only if it stays away, or the
            # app's navigation shell has appeared.
            def pw_gone() -> bool:
                return page.locator(pass_sel).count() == 0 or not page.locator(pass_sel).first.is_visible()
            try:
                if pw_gone():
                    if page.locator("nav, aside, [role=navigation]").count() and page.locator("nav, aside, [role=navigation]").first.is_visible():
                        return True
                    if rate_limited["answered"]:
                        page.wait_for_timeout(1500)
                        return pw_gone()
            except Exception:
                pass
            return False
        ok = False
        for _ in range(int(settings.nav_timeout_ms / 500) + 1):
            if _signed_in():
                ok = True
                break
            page.wait_for_timeout(500)
        if not ok:
            if rate_limited["hit"]:
                page.close()
                res = LoginResult(False, login_url, "login rate-limited by the server (HTTP 429, 'too many attempts'). The app enforces a login cooldown; wait and retry.", login_url=login_url)
                res.rate_limited, res.retry_after = True, rate_limited["retry_after"]
                return res
            if rate_limited["server_error"]:
                page.close()
                res = LoginResult(False, login_url, f"the site's sign-in endpoint answered HTTP {rate_limited['server_error']} (server error) — the app's backend is failing, not the credentials", login_url=login_url)
                res.server_error = rate_limited["server_error"]
                return res
            alert = page.locator("[role=alert]").first
            msg = alert.inner_text()[:200] if alert.count() and alert.is_visible() else "no sign of a session after submit (URL unchanged, password field still shown, no token stored)"
            page.close()
            return LoginResult(False, login_url, f"login failed: {msg}", login_url=login_url)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        page.wait_for_timeout(settings.settle_ms)
        final = page.url
        state_path = ""
        if save_state:
            state_path = str(run_dir / "storage_state.json")
            context.storage_state(path=state_path)
        page.close()
        return LoginResult(True, final, storage_state_path=state_path, login_url=login_url)
    except Exception as exc:
        try:
            page.close()
        except Exception:
            pass
        return LoginResult(False, login_url, f"login error: {type(exc).__name__}: {str(exc)[:200]}", login_url=login_url)


QUIET_JS = """() => {
  const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const busy = Array.from(document.querySelectorAll('[aria-busy="true"], [role=progressbar], [class*="spinner" i], [class*="skeleton" i], [class*="loader" i]')).some(vis)
    || Array.from(document.querySelectorAll('button, span, p, td, div')).some((el) => el.children.length === 0 && vis(el) && /^(loading|loading\\.\\.\\.|loading…|please wait)$/i.test((el.innerText || '').trim()));
  return { busy, resources: performance.getEntriesByType('resource').length };
}"""


def wait_for_data(page, max_ms: int = 6000) -> None:
    """After an in-page navigation (a tab click), wait until the screen's data has arrived:
    no loading indicator and no new network resource for two consecutive polls."""
    import time as _t
    deadline = _t.time() + max_ms / 1000
    last, stable = -1, 0
    while _t.time() < deadline:
        try:
            st = page.evaluate(QUIET_JS)
        except Exception:
            return
        stable = stable + 1 if (not st["busy"] and st["resources"] == last) else 0
        if stable >= 2:
            return
        last = st["resources"]
        page.wait_for_timeout(300)
