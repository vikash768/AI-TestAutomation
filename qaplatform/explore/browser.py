"""Playwright session, page monitors and the generic login flow."""
from __future__ import annotations

import logging
import os
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

    def reset(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()
        self.failed_requests.clear()
        self.api_requests.clear()
        self.dialogs.clear()
        self.crashed = False
        self.requests_seen = 0

    def snapshot(self) -> dict:
        return {
            "console_errors": list(dict.fromkeys(self.console_errors)),
            "page_errors": list(dict.fromkeys(self.page_errors)),
            "failed_requests": list(self.failed_requests),
            "api_requests": list(self.api_requests),
            "dialogs": list(self.dialogs),
            "crashed": self.crashed,
            "requests_seen": self.requests_seen,
        }


def attach_monitor(page: Page) -> Monitor:
    mon = Monitor()

    def on_console(msg):
        if msg.type in ("error",):
            text = msg.text[:300]
            if not any(p.search(text) for p in NOISE_PATTERNS):
                mon.console_errors.append(text)

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
                mon.failed_requests.append({"url": req.url[:300], "method": req.method, "status": None, "error": text[:120], "type": req.resource_type})

    def on_response(resp):
        try:
            if resp.status >= 400 and resp.request.resource_type in ("document", "xhr", "fetch", "script", "stylesheet", "image"):
                if not any(p.search(resp.url) for p in NOISE_PATTERNS):
                    mon.failed_requests.append({"url": resp.url[:300], "method": resp.request.method, "status": resp.status, "error": "", "type": resp.request.resource_type})
        except Exception:
            pass

    def on_dialog(dialog):
        mon.dialogs.append({"type": dialog.type, "message": dialog.message[:200]})
        try:
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
        try:
            page.wait_for_url(lambda u: "/login" not in urlparse(u).path, timeout=settings.nav_timeout_ms)
        except PWTimeout:
            alert = page.locator("[role=alert]").first
            msg = alert.inner_text()[:200] if alert.count() else "still on login page after submit"
            page.close()
            return LoginResult(False, page.url if not page.is_closed() else login_url, f"login failed: {msg}", login_url=login_url)
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
