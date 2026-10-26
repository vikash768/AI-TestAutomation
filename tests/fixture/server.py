"""Serve the fixture SPA and its JSON API inside a Playwright context (no sockets)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ORIGIN = "https://fixture.qa"
EMAIL = "admin@fixture.qa"
PASSWORD = "Fixture#2026"
HTML = (Path(__file__).parent / "app.html").read_text(encoding="utf-8")


class FixtureServer:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def install(self, ctx) -> None:
        ctx.route(re.compile(r"^https://fixture\.qa/.*"), self.handle)

    def handle(self, route) -> None:
        req = route.request
        path = req.url.split(ORIGIN, 1)[1].split("?")[0]
        self.calls.append((req.method, path))
        if not path.startswith("/api/"):
            return route.fulfill(status=200, content_type="text/html", body=HTML)
        if path == "/api/login" and req.method == "POST":
            body = req.post_data_json or {}
            if body.get("email") == EMAIL and body.get("password") == PASSWORD:
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"token": "t-123"}))
            return route.fulfill(status=401, content_type="application/json", body=json.dumps({"message": "Invalid email or password."}))
        if path == "/api/items" and req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [{"name": "Alpha"}, {"name": "Beta"}]}))
        if path == "/api/settings" and req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"settings": {"tz": "UTC", "iv": 10}}))
        if path == "/api/live":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"active": []}))
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
