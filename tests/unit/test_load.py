"""Load engine against a local threaded HTTP server only: caps, auth, 5xx, slowdown, 429, abort, no secret leakage."""
from __future__ import annotations

import json
import socketserver
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from qaplatform.execute.load import _targets, collapse, run_load

TOKEN = "Bearer tok-7f3a9c-SEKRET"


class _Srv:
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = self.max_inflight = self.total = self.err_n = self.slow_inflight = 0
        self.paths: dict[str, int] = {}
        self.auth_seen: list[str] = []


def _handler(srv: _Srv):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes = b"{}"):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            with srv.lock:
                srv.inflight += 1
                srv.total += 1
                srv.max_inflight = max(srv.max_inflight, srv.inflight)
                srv.paths[path] = srv.paths.get(path, 0) + 1
                if self.headers.get("Authorization"):
                    srv.auth_seen.append(path)
            try:
                if path == "/api/slow":
                    with srv.lock:
                        srv.slow_inflight += 1
                        n = srv.slow_inflight
                    time.sleep(0.02 + 0.15 * n)  # latency grows with concurrent requests
                    with srv.lock:
                        srv.slow_inflight -= 1
                    self._send(200)
                elif path == "/api/err":
                    with srv.lock:
                        srv.err_n += 1
                        bad = srv.err_n % 2 == 0
                    self._send(500 if bad else 200)
                elif path == "/api/down":
                    self._send(503)
                elif path == "/api/private":
                    self._send(200 if self.headers.get("Authorization") == TOKEN else 401)
                elif path == "/api/limited":
                    self._send(429)
                else:
                    self._send(200, b"<html>ok</html>")
            finally:
                with srv.lock:
                    srv.inflight -= 1
    return H


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # HTTPServer's getfqdn() reverse-DNS lookup can stall for ~30 s on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@pytest.fixture
def server():
    srv = _Srv()
    httpd = _Server(("127.0.0.1", 0), _handler(srv))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    srv.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield srv
    httpd.shutdown()
    httpd.server_close()


def _settings(conc=8, reqs=300, secs=8):
    return SimpleNamespace(load_max_concurrency=conc, load_max_requests=reqs, load_max_seconds=secs)


def _pages(base, *paths, method="GET"):
    return [SimpleNamespace(url=base + "/", protected=True, api_requests=[{"method": method, "url": base + p, "path": p} for p in paths])]


def _run(srv, paths, settings=None, headers=None, **kw):
    msgs = []
    kw.setdefault("users", 8)
    kw.setdefault("seconds", 3)
    tests, findings, stats = run_load(settings or _settings(), srv.base, _pages(srv.base, *paths), srv.base, headers or {},
                                      lambda stage, msg, progress=None, **k: msgs.append(msg), **kw)
    return tests, findings, stats, msgs


def _ep(stats, path):
    return next(e for e in stats["endpoints"] if e["path"] == path)


def test_targets_same_origin_get_only_collapsed_and_capped():
    base = "http://app.local"
    reqs = [{"method": "GET", "url": f"{base}/api/items/{i}", "path": f"/api/items/{i}"} for i in range(5)]
    reqs += [{"method": "POST", "url": f"{base}/api/items", "path": "/api/items"},
             {"method": "GET", "url": f"{base}/api/auth/me", "path": "/api/auth/me"},
             {"method": "GET", "url": f"{base}/api/logout", "path": "/api/logout"},
             {"method": "GET", "url": "http://evil.example/api/x", "path": "/api/x"},
             {"method": "GET", "url": "http://api.local/v1/authors", "path": "/v1/authors"}]
    reqs += [{"method": "GET", "url": f"{base}/api/r{i}", "path": f"/api/r{i}"} for i in range(20)]
    ts = _targets(base, [SimpleNamespace(url=base, protected=True, api_requests=reqs)], "http://api.local")
    paths = [t.path for t in ts]
    assert paths[0] == "/" and ts[0].kind == "page" and not ts[0].auth
    assert paths.count("/api/items/:id") == 1
    assert "/v1/authors" in paths  # api host is same-app; "authors" is not an auth path
    assert not any(p in paths for p in ("/api/items", "/api/auth/me", "/api/logout", "/api/x"))
    assert len(ts) == 8
    assert collapse("/a/550e8400-e29b-41d4-a716-446655440000/b") == "/a/:id/b"


def test_caps_requests_and_concurrency(server):
    tests, findings, stats, msgs = _run(server, ["/api/fast"], settings=_settings(conc=8, reqs=60, secs=4), users=50, seconds=60)
    assert stats["users"] == 8 and stats["seconds"] == 4
    assert server.total <= 60 and stats["requests"] == server.total
    assert sum(s["requests"] for s in stats["stages"]) <= 60
    assert server.max_inflight <= 8
    assert [s["users"] for s in stats["stages"]][:2] == [1, 2]
    assert any(m.startswith("Load stage 1/4: 1 users") for m in msgs)


def test_auth_header_measures_protected_endpoint(server):
    tests, findings, stats, msgs = _run(server, ["/api/private"], headers={"Authorization": TOKEN}, auth_kind="authorization header")
    ep = _ep(stats, "/api/private")
    assert ep["measurable"] and ep["auth_errors"] == 0 and ep["ok"] > 0
    assert stats["verdict"] == "pass" and not findings
    assert "/" not in server.auth_seen  # the public page document never gets the credentials
    blob = json.dumps([stats, [asdict(t) for t in tests], [asdict(f) for f in findings], msgs])
    assert "tok-7f3a9c" not in blob and "SEKRET" not in blob


def test_missing_auth_is_not_measurable_and_no_finding(server):
    tests, findings, stats, _ = _run(server, ["/api/private"])
    ep = _ep(stats, "/api/private")
    assert not ep["measurable"] and ep["auth_errors"] == ep["requests"] > 0
    assert stats["verdict"] == "not_measurable"
    assert next(t for t in tests if t.route == "/api/private").status == "blocked"
    assert next(t for t in tests if t.test_key == "load|ramp").status == "blocked"
    assert findings == []


def test_cookie_value_never_leaks(server):
    secret = "sid=cookie-9b1e-SEKRET"
    tests, findings, stats, msgs = _run(server, ["/api/fast", "/api/err"], headers={"Cookie": secret}, auth_kind="session cookie")
    blob = json.dumps([stats, [asdict(t) for t in tests], [asdict(f) for f in findings], msgs])
    assert "cookie-9b1e" not in blob and "SEKRET" not in blob


def test_server_errors_give_high_finding(server):
    tests, findings, stats, _ = _run(server, ["/api/fast", "/api/err"])
    assert not stats["aborted"]
    f = next(f for f in findings if f.route == "/api/err")
    assert f.severity == "high" and f.status == "reported" and "returns server errors under load" in f.title
    assert next(t for t in tests if t.route == "/api/err").status == "fail"
    assert next(t for t in tests if t.route == "/api/fast").status == "pass"
    assert stats["verdict"] == "fail"
    assert not any(f.route == "/api/fast" for f in findings)


def test_slowdown_is_degraded(server):
    tests, findings, stats, _ = _run(server, ["/api/slow"], seconds=6, p95_ms=600)
    assert stats["verdict"] == "degraded", stats["stages"]
    assert stats["knee_users"] and stats["knee_users"] > 1
    f = next(f for f in findings if f.route == "/api/slow")
    assert f.severity == "medium" and "slows down under load" in f.title and "at 1)" in f.title


def test_rate_limit_needs_review(server):
    tests, findings, stats, _ = _run(server, ["/api/limited"], seconds=2)
    f = next(f for f in findings if "rate-limits normal read traffic" in f.title)
    assert f.status == "needs_review" and f.severity == "medium"
    assert next(t for t in tests if t.route == "/api/limited").status == "blocked"
    assert stats["verdict"] != "pass"


def test_aborts_on_heavy_server_errors(server):
    tests, findings, stats, _ = _run(server, ["/api/down"], seconds=8)
    assert stats["aborted"] and "server-error rate" in stats["abort_reason"]
    assert stats["verdict"] == "fail"
    assert len(stats["stages"]) < 4 and server.total < 80
    assert any(f.route == "/api/down" and f.severity == "high" for f in findings)


def test_get_only_never_writes(server):
    _, _, stats = run_load(_settings(), server.base, _pages(server.base, "/api/fast", method="POST"), server.base, {}, lambda *a, **k: None,
                           users=2, seconds=1)
    assert "/api/fast" not in server.paths
    assert [e["path"] for e in stats["endpoints"]] == ["/"]  # only POSTs captured -> nothing but the page


def test_time_cap_holds_even_when_every_request_is_slow():
    """A server that takes 4 s per request must not stretch a 4 s load test to 16 s."""
    import threading
    import time as _t
    import types
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from qaplatform.execute.load import run_load

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            _t.sleep(4)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    class Srv(ThreadingHTTPServer):
        def server_bind(self):  # skip the reverse-DNS lookup that stalls on macOS
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    srv = Srv(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        page = types.SimpleNamespace(url=base + "/", protected=True, api_requests=[{"method": "GET", "url": base + "/api/slow", "path": "/api/slow"}])
        st = types.SimpleNamespace(load_max_concurrency=8, load_max_requests=2000, load_max_seconds=4)
        t0 = _t.time()
        run_load(st, base, [page], base, {}, lambda *a, **k: None, users=8, seconds=4, p95_ms=2000)
        assert _t.time() - t0 < 9  # cap plus grace, never cap + stages x timeout
    finally:
        srv.shutdown()


def _cap_settings(users=15, reqs=5000):
    import types
    return types.SimpleNamespace(load_max_concurrency=8, load_max_requests=300, load_max_seconds=8, capacity_max_users=users, capacity_max_requests=reqs)


def test_capacity_finds_the_breaking_point_and_stops_there(server):
    """/api/slow gets slower with concurrency: the step where p95 passes the target is the breaking point."""
    tests, findings, stats, msgs = _run(server, ["/api/slow"], settings=_cap_settings(), mode="capacity", users=15, step_seconds=5, p95_ms=300, expected_users=20)
    cap = stats["capacity"]
    bp = cap["breaking_point"]
    assert bp and bp["users"] > 1 and bp["p95"] > 300 and bp["p50"] is not None
    assert cap["last_healthy"] and cap["last_healthy"]["users"] < bp["users"] and cap["last_healthy"]["p95"] <= 300
    assert stats["stages"][-1]["users"] == bp["users"]  # nothing was sent past the failing step
    assert any("Breaking point:" in m for m in msgs)
    t = next(t for t in tests if t.test_key == "load|capacity")
    assert t.status == "fail" and f"breaks at {bp['users']}" in t.observed
    assert [f.title for f in findings if "breaks at" in f.title]  # expected 20 users, broke earlier
    assert not [f for f in findings if "slows down" in f.title]  # the intended slowdown is not a per-endpoint bug


def test_capacity_reports_no_breaking_point_when_it_holds(server):
    tests, findings, stats, _ = _run(server, ["/api/fast"], settings=_cap_settings(users=5), mode="capacity", users=5, step_seconds=5, p95_ms=2000)
    cap = stats["capacity"]
    assert cap["breaking_point"] is None and cap["max_tested_users"] == 5
    assert cap["last_healthy"]["p50"] is not None and cap["last_healthy"]["p95"] is not None
    assert next(t for t in tests if t.test_key == "load|capacity").status == "pass" and not findings


def test_capacity_summary_on_the_life_sadx_profile():
    """Recorded Life Sadx steps: flat to 50 users, p95 x8 at 75, throughput falls at 100."""
    from qaplatform.execute.load import capacity_summary
    rows = [(1, 4.07, 42.8, 52.6), (2, 8.26, 41.7, 52.5), (5, 20.46, 40.6, 50.6), (10, 40.78, 40.7, 53.1), (15, 61.11, 43.2, 59.4), (20, 81.15, 44.1, 59.2),
            (30, 120.4, 46.3, 61.4), (40, 159.22, 46.7, 58.3), (50, 196.61, 46.6, 62.9), (75, 229.86, 48.4, 527.8), (100, 133.79, 356.4, 1527.8)]
    stages = [{"users": u, "requests": 100, "rps": rps, "p50": p50, "p95": p95, "error_rate": 0.0} for u, rps, p50, p95 in rows]
    cap = capacity_summary(stages, 2000, 15.0)
    assert cap["breaking_point"]["users"] == 100 and "throughput fell" in cap["breaking_point"]["reason"]
    assert cap["knee"]["users"] == 75
    assert cap["last_healthy"]["users"] == 50 and cap["last_healthy"]["p95"] == 62.9
