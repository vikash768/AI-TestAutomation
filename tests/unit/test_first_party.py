"""Errors from other companies' scripts/resources are information, not the site's bug."""
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if "missing" in self.path:
            self.send_response(404); self.end_headers(); return
        body = (b'<!doctype html><html><body><h1>Site</h1>'
                b'<img src="/missing-own.png">'                       # first-party broken image
                b'<img src="http://localhost:%d/missing-3p.png">'     # third-party broken image (other host)
                b'<script>console.error("our own script failed")</script>'
                b'</body></html>') % self.server.server_address[1]
        self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


class S(ThreadingHTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@pytest.fixture()
def site():
    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def test_third_party_errors_are_separated_from_the_sites_own(site):
    from playwright.sync_api import sync_playwright
    from qaplatform.explore.browser import attach_monitor
    pw = sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {exc}")
        page = b.new_page()
        mon = attach_monitor(page, site)
        page.goto(site, wait_until="networkidle")
        page.wait_for_timeout(300)
        snap = mon.snapshot()
        own_failed = [r for r in snap["failed_requests"] if "missing-own" in r["url"]]
        tp_failed = [r for r in snap["third_party_failed"] if "missing-3p" in r["url"]]
        assert own_failed, "first-party broken image should be a finding"
        assert tp_failed, "third-party broken image should be separated out"
        # the third-party image must NOT be in the site's own failed_requests
        assert not [r for r in snap["failed_requests"] if "missing-3p" in r["url"]]
        # our own console error stays first-party
        assert any("our own script failed" in e for e in snap["console_errors"])
        b.close()
    finally:
        pw.stop()
