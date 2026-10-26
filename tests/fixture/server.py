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


CRUD_ORIGIN = "https://crud.qa"
CRUD_HTML = (Path(__file__).parent / "crud_app.html").read_text(encoding="utf-8")


class CrudServer:
    """In-browser CRUD backend. Seeds one REAL member that tests must never delete."""

    def __init__(self):
        self.members = {"owner_real": {"username": "owner_real", "name": "Real Owner", "status": "active"}}
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def install(self, ctx) -> None:
        ctx.route(re.compile(r"^https://crud\.qa/.*"), self.handle)

    def handle(self, route) -> None:
        req = route.request
        path = req.url.split(CRUD_ORIGIN, 1)[1].split("?")[0]
        self.calls.append((req.method, path))
        if not path.startswith("/api/"):
            return route.fulfill(status=200, content_type="text/html", body=CRUD_HTML)
        if path == "/api/login" and req.method == "POST":
            b = req.post_data_json or {}
            if b.get("email") == EMAIL and b.get("password") == PASSWORD:
                return route.fulfill(status=200, content_type="application/json", body=json.dumps({"token": "t-crud"}))
            return route.fulfill(status=401, content_type="application/json", body=json.dumps({"message": "bad creds"}))
        if path == "/api/members" and req.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"members": list(self.members.values())}))
        if path == "/api/members" and req.method == "POST":
            b = req.post_data_json or {}
            u = b.get("username", "")
            self.members[u] = {"username": u, "name": b.get("name", ""), "status": "active"}
            return route.fulfill(status=201, content_type="application/json", body=json.dumps({"ok": True}))
        m = re.match(r"^/api/members/([^/]+)/toggle$", path)
        if m and req.method == "POST":
            u = re.sub(r"%40", "@", m.group(1))
            if u in self.members:
                self.members[u]["status"] = "inactive" if self.members[u]["status"] == "active" else "active"
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        m = re.match(r"^/api/members/([^/]+)$", path)
        if m and req.method == "DELETE":
            u = m.group(1)
            self.deleted.append(u)
            self.members.pop(u, None)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        return route.fulfill(status=404, content_type="application/json", body=json.dumps({"message": "not found"}))
