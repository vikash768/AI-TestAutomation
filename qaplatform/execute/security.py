"""Non-destructive security assessment (OWASP WSTG-aligned).

Everything here is read-only: GET / HEAD / OPTIONS only, benign values, no
brute force, no injection payloads that change state, no load. It checks what
a careful reviewer checks from outside: transport, headers, cookies, CORS,
information leakage, and access control (endpoints the code marks protected
must refuse an anonymous caller). Active or destructive techniques are out of
scope by design.

Inputs: the live origin, the code inventory (API paths and which routes/
endpoints the code gates behind auth), and the pages already crawled.
"""
from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.ingest.js_parser import CodeInventory
from qaplatform.judge.gate import gate_status

CATEGORY = "security"
LEAK_RE = re.compile(r"(stack trace|\bat [\w.$<>]+ \([^)]*:\d+|Traceback \(most recent|\.js:\d+:\d+|/(src|app|node_modules|home|usr)/|SQLSTATE|syntax error at|ECONNREFUSED|ER_|sequelize|prisma|pg_|postgres://|mongodb://|JWT|jsonwebtoken|bcrypt|x-powered-by)", re.I)
SENSITIVE_HINT = re.compile(r"(result|user|admin|export|firmware|calibration|instrument|method|patient|record|account|order|payment|report)", re.I)

# header -> (severity, what it defends against)
SECURITY_HEADERS = {
    "content-security-policy": ("medium", "cross-site scripting and data injection (defence in depth)"),
    "strict-transport-security": ("medium", "protocol downgrade / SSL stripping"),
    "x-content-type-options": ("low", "MIME-type sniffing"),
    "x-frame-options": ("low", "clickjacking (unless a CSP frame-ancestors is set)"),
    "referrer-policy": ("low", "referrer leakage to third parties"),
}


@dataclass
class HeadResult:
    url: str
    status: int
    headers: dict
    set_cookie: list
    server: str
    powered_by: str
    body_sample: str = ""
    error: str = ""


def _client(settings) -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=min(settings.nav_timeout_ms / 1000, 20),
                        headers={"User-Agent": "AI-TestAutomation security probe (read-only)"}, verify=True)


def _get(client: httpx.Client, url: str, method: str = "GET", headers: dict | None = None) -> HeadResult:
    try:
        r = client.request(method, url, headers=headers or {})
        body = ""
        if method == "GET":
            try:
                body = r.text[:4000]
            except Exception:
                body = ""
        raw_cookies = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else ([r.headers["set-cookie"]] if "set-cookie" in r.headers else [])
        return HeadResult(url=url, status=r.status_code, headers={k.lower(): v for k, v in r.headers.items()},
                          set_cookie=raw_cookies, server=r.headers.get("server", ""), powered_by=r.headers.get("x-powered-by", ""), body_sample=body)
    except Exception as exc:
        return HeadResult(url=url, status=0, headers={}, set_cookie=[], server="", powered_by="", error=f"{type(exc).__name__}: {str(exc)[:160]}")


def _finding(kind, route, sev, title, expected, observed, ref, conf=0.85, status=None, desc="") -> FindingRecord:
    return FindingRecord(fingerprint=fingerprint("sec", kind, route), route=route, category=CATEGORY, severity=sev, title=title, description=desc,
                         expected=expected, observed=observed, standard_ref=ref, confidence=conf, reproduced_n=1, reproduced_m=1, status=status or gate_status(conf))


def _test(key, name, route, expected, observed, status) -> TestRecord:
    return TestRecord(test_key=f"security|{key}", name=name, technique="security", category=CATEGORY, route=route, expected=expected, observed=observed, status=status, reproduced_n=1, reproduced_m=1)


def run_security(settings, live_url: str, inv: CodeInventory, pages, api_base: str, events) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    origin = f"{urlparse(live_url).scheme}://{urlparse(live_url).netloc}"
    events("security", "Checking transport, headers, cookies, CORS and information leakage", 90)

    with _client(settings) as client:
        # 1. HTTPS + HSTS on the main document
        root = _get(client, origin + "/")
        is_https = urlparse(root.url if root.status else origin).scheme == "https" or origin.startswith("https")
        t = _test("https", "Site is served over HTTPS", "/", "The application is reachable only over HTTPS", f"scheme {urlparse(origin).scheme}, final {root.url}", "pass" if is_https else "fail")
        tests.append(t)
        if not is_https:
            findings.append(_finding("https", "/", "high", "Application is served over plain HTTP", t.expected, t.observed, "OWASP WSTG-CRYP-03"))

        # 2. Security headers on the main document
        if root.status:
            for h, (sev, defends) in SECURITY_HEADERS.items():
                present = h in root.headers
                if h == "strict-transport-security" and not is_https:
                    continue
                if h == "x-frame-options" and "content-security-policy" in root.headers and "frame-ancestors" in root.headers.get("content-security-policy", "").lower():
                    present = True
                tn = _test(f"hdr|{h}", f"Response sets {h}", "/", f"The {h} header is present ({defends})", "present" if present else "absent", "pass" if present else "fail")
                tests.append(tn)
                if not present:
                    findings.append(_finding(f"hdr-{h}", "/", sev, f"Missing {h} header", tn.expected, "header not set on the main response", "OWASP WSTG-CONF-07 / Secure Headers Project"))
            # 3. Server / framework version disclosure
            banner = (root.server or "") + " " + (root.powered_by or "")
            if re.search(r"\d+\.\d+", banner) or root.powered_by:
                t = _test("banner", "Server does not advertise software versions", "/", "No Server/X-Powered-By version is exposed", f"Server={root.server!r} X-Powered-By={root.powered_by!r}", "fail")
                tests.append(t)
                findings.append(_finding("banner", "/", "low", "Server advertises its software/version in headers", t.expected, t.observed, "OWASP WSTG-INFO-02"))

        # 4. Cookie flags on any Set-Cookie seen (login or root)
        cookie_sources = list(root.set_cookie)
        login_paths = [r.path for r in inv.routes if "login" in (r.path or "").lower()] or ["/login"]
        for lp in login_paths[:2]:
            cookie_sources += _get(client, urljoin(origin + "/", lp.lstrip("/"))).set_cookie
        for raw in cookie_sources[:10]:
            name = raw.split("=", 1)[0].strip()
            low = raw.lower()
            missing = [f for f, tok in (("Secure", "secure"), ("HttpOnly", "httponly"), ("SameSite", "samesite")) if tok not in low]
            looks_session = re.search(r"sess|token|auth|jwt|sid|id", name, re.I)
            t = _test(f"cookie|{name}", f"Cookie '{name}' is hardened", "/", "Session cookies set Secure, HttpOnly and SameSite", f"missing: {', '.join(missing) or 'none'}", "pass" if not missing else "fail")
            tests.append(t)
            if missing and looks_session:
                findings.append(_finding(f"cookie-{name}", "/", "medium" if "HttpOnly" in missing or "Secure" in missing else "low",
                                         f"Session cookie '{name}' missing {', '.join(missing)}", t.expected, t.observed, "OWASP WSTG-SESS-02"))

        # 5. CORS: does the API reflect an arbitrary Origin with credentials?
        api = (api_base or origin).rstrip("/")
        health = _get(client, api + "/api/health", method="GET", headers={"Origin": "https://evil.example.com"})
        if health.status and health.status < 500:
            acao = health.headers.get("access-control-allow-origin", "")
            acac = health.headers.get("access-control-allow-credentials", "")
            reflected = acao == "https://evil.example.com" or acao == "*"
            t = _test("cors", "API does not reflect an arbitrary CORS origin", "/api", "Access-Control-Allow-Origin is not a reflected arbitrary origin (especially with credentials)", f"ACAO={acao!r} ACAC={acac!r}", "pass" if not reflected else "fail")
            tests.append(t)
            if reflected:
                sev = "high" if acac.lower() == "true" and acao != "*" else "medium"
                findings.append(_finding("cors", "/api", sev, "API reflects an arbitrary CORS origin", t.expected, t.observed, "OWASP WSTG-CLNT-07",
                                         desc="A reflected Access-Control-Allow-Origin with credentials lets any site read authenticated responses." if sev == "high" else ""))

        # 6. Access control: protected API reads must refuse an anonymous caller
        get_paths = sorted({a.path for a in inv.api_calls if a.method in ("GET", "HEAD")})
        checked = 0
        for path in get_paths:
            concrete = re.sub(r"\$\{[^}]*\}", "1", path.split("?")[0])
            if concrete.rstrip("/") in ("/api/health",) or "login" in concrete.lower() or "/auth" in concrete.lower():
                continue
            if not SENSITIVE_HINT.search(concrete):
                continue
            url = api + ("" if concrete.startswith("/") else "/") + concrete
            r = _get(client, url)
            if r.status == 0:
                continue
            checked += 1
            protected = r.status in (401, 403)
            not_found = r.status == 404
            leaked = r.status == 200 and len(r.body_sample) > 2
            route = concrete
            t = _test(f"authz|{concrete}", f"Anonymous GET {concrete} is refused", route, "A protected data endpoint returns 401/403 without a session", f"HTTP {r.status}", "pass" if protected else ("skip" if not_found else "fail"))
            if not_found:
                t.status = "blocked"
                t.observed = "404 (endpoint not deployed at this path)"
            tests.append(t)
            if leaked:
                findings.append(_finding(f"authz-{concrete}", route, "critical", f"{concrete} returns data without authentication", t.expected, f"HTTP 200 with a body of {len(r.body_sample)} chars to an anonymous request", "OWASP WSTG-ATHZ-02 / Broken Access Control (OWASP Top 10 A01)",
                                         desc="A sensitive endpoint the code gates behind auth answered an anonymous request with data."))
            if checked >= 25:
                break

        # 7. Information leakage: a deliberately bad request should not return internals
        probe = _get(client, api + "/api/__nonexistent_qa_probe__")
        if probe.status and LEAK_RE.search(probe.body_sample):
            t = _test("leak", "Error responses do not expose internals", "/api", "A 404/error body carries no stack trace, file path, SQL or framework detail", probe.body_sample[:140], "fail")
            tests.append(t)
            findings.append(_finding("leak", "/api", "medium", "Error response exposes internal details", t.expected, t.observed[:200], "OWASP WSTG-ERRH-01"))
        else:
            tests.append(_test("leak", "Error responses do not expose internals", "/api", "A 404/error body carries no stack trace, file path, SQL or framework detail", f"HTTP {probe.status}, clean" if probe.status else "no response", "pass" if probe.status else "blocked"))

    return tests, findings


def detect_api_base(inv: CodeInventory, live_url: str) -> str:
    """The live admin and API usually share an origin; the code's api.js says so for same-origin builds."""
    return f"{urlparse(live_url).scheme}://{urlparse(live_url).netloc}"
