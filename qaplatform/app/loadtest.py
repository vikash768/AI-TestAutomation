"""Backend API load test for native runs.

The load goes to the app's backend over HTTP, never through Appium sessions.
It is bounded and non-destructive by construction:

  * GET only: anything else is listed as skipped, never sent
  * every endpoint is probed once first; 401/403 (needs a session) and 404 are skipped
  * concurrency, rate and duration are capped (LOAD_TEST_* settings, hard limits below)
  * one shared rate limiter for all virtual users, so max_rps holds overall
  * no target, or no safe endpoint, means status "blocked" with the reason

Results: per endpoint total / ok / failed requests, error rate, requests per
second, p50 / p95 / p99 / max latency, plus tests and findings for the report.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from qaplatform.app.backend import BackendRoute, ClientEndpoint
from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.judge.gate import gate_status

HARD_MAX_USERS = 50
HARD_MAX_RPS = 100
HARD_MAX_DURATION_S = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class LoadConfig:
    enabled: bool = False
    base_url: str = ""
    users: int = 10
    duration_s: int = 30
    max_rps: int = 20
    timeout_s: float = 10.0
    p95_limit_ms: int = 1000
    max_error_rate: float = 0.01
    max_endpoints: int = 3
    endpoints: list[str] = field(default_factory=list)  # explicit "/api/x" paths (GET); else discovered
    bearer_token: str = ""  # optional token for endpoints behind sign-in (never logged)

    def capped(self) -> "LoadConfig":
        self.users = max(1, min(self.users, HARD_MAX_USERS))
        self.max_rps = max(1, min(self.max_rps, HARD_MAX_RPS))
        self.duration_s = max(5, min(self.duration_s, HARD_MAX_DURATION_S))
        self.max_endpoints = max(1, min(self.max_endpoints, 10))
        return self

    @classmethod
    def from_env(cls, options: dict | None = None) -> "LoadConfig":
        options = options or {}
        env_on = (os.environ.get("LOAD_TEST_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            enabled=bool(options.get("load_test", env_on)),
            base_url=(options.get("load_test_base_url") or os.environ.get("LOAD_TEST_BASE_URL") or "").strip().rstrip("/"),
            users=_env_int("LOAD_TEST_USERS", 10), duration_s=_env_int("LOAD_TEST_DURATION_SECONDS", 30), max_rps=_env_int("LOAD_TEST_MAX_RPS", 20),
            timeout_s=_env_float("LOAD_TEST_TIMEOUT_SECONDS", 10.0), p95_limit_ms=_env_int("LOAD_TEST_P95_MS", 1000),
            max_error_rate=_env_float("LOAD_TEST_MAX_ERROR_RATE", 0.01), max_endpoints=_env_int("LOAD_TEST_MAX_ENDPOINTS", 3),
            endpoints=[e.strip() for e in (os.environ.get("LOAD_TEST_ENDPOINTS") or "").split(",") if e.strip()],
            bearer_token=os.environ.get("LOAD_TEST_BEARER_TOKEN") or "",
        ).capped()


def choose_targets(cfg: LoadConfig, routes: list[BackendRoute], client_eps: list[ClientEndpoint]) -> tuple[list[dict], list[str]]:
    """GET endpoints without path parameters: those the app calls first, then backend routes; everything else is skipped with a reason."""
    targets: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()

    def consider(method: str, path: str, source: str, auth: bool | None = None, dynamic: bool = False) -> None:
        path = "/" + path.lstrip("/")
        if path in seen:
            return
        seen.add(path)
        if method not in ("GET", "ANY"):
            skipped.append(f"{method} {path}: not idempotent, never load tested")
        elif dynamic or "{" in path or "/:" in path or "<" in path:
            skipped.append(f"GET {path}: needs an id known only at run time")
        elif auth and not cfg.bearer_token:
            skipped.append(f"GET {path}: behind sign-in and no LOAD_TEST_BEARER_TOKEN is set")
        elif len(targets) >= cfg.max_endpoints:
            skipped.append(f"GET {path}: over LOAD_TEST_MAX_ENDPOINTS={cfg.max_endpoints}")
        else:
            targets.append({"method": "GET", "path": path, "source": source, "auth": auth})

    if cfg.endpoints:
        for e in cfg.endpoints:
            method, _, path = e.partition(" ") if " " in e else ("GET", "", e)
            consider(method.upper(), path, "LOAD_TEST_ENDPOINTS")
        return targets, skipped
    auth_by_path = {r.path: r.auth for r in routes}
    for e in client_eps:
        consider(e.method, e.path, f"app code {e.file}:{e.line}", auth_by_path.get(e.path), e.dynamic)
    for r in routes:
        consider(r.method, r.path, f"backend {r.file}:{r.line}", r.auth)
    return targets, skipped


def _pct(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return 0.0
    k = min(len(sorted_ms) - 1, max(0, int(round(p / 100 * len(sorted_ms) + 0.5)) - 1))
    return round(sorted_ms[k], 1)


class _Limiter:
    """Hands out evenly spaced send times: at most `rps` requests per second across all users."""

    def __init__(self, rps: int):
        self.interval = 1.0 / rps
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, deadline: float) -> bool:
        with self.lock:
            slot = max(self.next, time.monotonic())
            self.next = slot + self.interval
        if slot >= deadline:
            return False
        time.sleep(max(0.0, slot - time.monotonic()))
        return True


def load_endpoint(client, url: str, users: int, rps: int, duration_s: float, headers: dict) -> dict:
    lat: list[float] = []
    codes: dict[str, int] = {}
    errors: list[str] = []
    lock = threading.Lock()
    limiter = _Limiter(rps)
    start = time.monotonic()
    deadline = start + duration_s

    def user() -> None:
        while limiter.wait(deadline):
            t0 = time.monotonic()
            try:
                r = client.get(url, headers=headers)
                code, err = str(r.status_code), ""
            except Exception as exc:
                code, err = "error", f"{type(exc).__name__}: {str(exc)[:120]}"
            ms = (time.monotonic() - t0) * 1000
            with lock:
                lat.append(ms) if code != "error" else None
                codes[code] = codes.get(code, 0) + 1
                if err and len(errors) < 5:
                    errors.append(err)

    threads = [threading.Thread(target=user, daemon=True) for _ in range(users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_s + 30)
    elapsed = max(0.001, time.monotonic() - start)
    total = sum(codes.values())
    ok = sum(n for c, n in codes.items() if c.isdigit() and 200 <= int(c) < 400)
    lat.sort()
    return {"total": total, "ok": ok, "failed": total - ok, "error_rate": round((total - ok) / total, 4) if total else 0.0, "rps": round(total / elapsed, 2),
            "p50_ms": _pct(lat, 50), "p95_ms": _pct(lat, 95), "p99_ms": _pct(lat, 99), "max_ms": round(lat[-1], 1) if lat else 0.0,
            "duration_s": round(elapsed, 1), "status_codes": codes, "errors": errors}


def run_load_test(cfg: LoadConfig, routes: list[BackendRoute], client_eps: list[ClientEndpoint], events, run_id: str = "",
                  discovered_bases: list[str] | None = None) -> tuple[dict, list[TestRecord], list[FindingRecord]]:
    import httpx

    summary: dict = {"status": "not run", "users": cfg.users, "max_rps": cfg.max_rps, "duration_s": cfg.duration_s, "p95_limit_ms": cfg.p95_limit_ms,
                     "max_error_rate": cfg.max_error_rate, "endpoints": [], "skipped": []}
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    if not cfg.enabled:
        summary.update(status="disabled", reason="not requested (form checkbox or LOAD_TEST_ENABLED)")
        return summary, tests, findings
    base = cfg.base_url or (discovered_bases or [""])[0]
    summary["base_url"] = base
    summary["base_url_source"] = "configured" if cfg.base_url else ("found in the app's code" if base else "")
    parsed = urlparse(base)
    if not base or parsed.scheme not in ("http", "https") or not parsed.netloc:
        summary.update(status="blocked", reason="no backend API base URL: set LOAD_TEST_BASE_URL or the form field")
        tests.append(TestRecord(test_key="load|config", name="Backend API load test", technique="load", category="performance", route="backend",
                                expected="A backend base URL to load", observed=summary["reason"], status="blocked"))
        return summary, tests, findings
    targets, skipped = choose_targets(cfg, routes, client_eps)
    summary["skipped"] = skipped
    if not targets:
        summary.update(status="blocked", reason="no safe endpoint: only GET endpoints without ids are load tested")
        tests.append(TestRecord(test_key="load|targets", name="Backend API load test", technique="load", category="performance", route="backend",
                                expected="At least one GET endpoint to load", observed=summary["reason"], status="blocked"))
        return summary, tests, findings
    headers = {"User-Agent": f"qaplatform-load-test/{run_id or 'run'}", "Accept": "application/json"}
    per_endpoint = max(5.0, cfg.duration_s / len(targets))
    events("loadtest", f"Load testing {len(targets)} GET endpoint(s) on {base}: {cfg.users} users, at most {cfg.max_rps} req/s, {per_endpoint:.0f} s each", None)
    limits = httpx.Limits(max_connections=cfg.users, max_keepalive_connections=cfg.users)
    with httpx.Client(timeout=cfg.timeout_s, limits=limits, follow_redirects=False) as client:
        for t in targets:
            url = base + t["path"]
            h = dict(headers)
            if t.get("auth") and cfg.bearer_token:
                h["Authorization"] = f"Bearer {cfg.bearer_token}"
            try:
                probe = client.get(url, headers=h)
                pcode = probe.status_code
            except Exception as exc:
                summary["skipped"].append(f"GET {t['path']}: unreachable ({type(exc).__name__})")
                continue
            if pcode in (401, 403):
                summary["skipped"].append(f"GET {t['path']}: answers {pcode} (needs a session)")
                continue
            if pcode == 404 or 300 <= pcode < 400:
                summary["skipped"].append(f"GET {t['path']}: answers {pcode} at this base URL")
                continue
            m = load_endpoint(client, url, cfg.users, cfg.max_rps, per_endpoint, h)
            slow = m["p95_ms"] > cfg.p95_limit_ms
            erring = m["error_rate"] > cfg.max_error_rate
            status = "fail" if (slow or erring) else "pass"
            ep = {"method": "GET", "path": t["path"], "url": url, "source": t["source"], "status": status, "probe_status": pcode, **m}
            summary["endpoints"].append(ep)
            key = f"load|GET {t['path']}"
            observed = (f"{m['total']} requests, {m['failed']} failed ({m['error_rate'] * 100:.1f}%), {m['rps']} req/s, p50 {m['p50_ms']} ms, p95 {m['p95_ms']} ms, "
                        f"p99 {m['p99_ms']} ms, max {m['max_ms']} ms; codes {m['status_codes']}" + (f"; errors: {m['errors'][0]}" if m["errors"] else ""))
            tests.append(TestRecord(test_key=key, name=f"Load: GET {t['path']} ({cfg.users} users, ≤{cfg.max_rps} req/s, {m['duration_s']} s)", technique="load",
                                    category="performance", route="backend", steps=[{"text": f"{cfg.users} concurrent users send GET {url} for {per_endpoint:.0f} s, at most {cfg.max_rps} req/s overall"}],
                                    expected=f"Error rate ≤ {cfg.max_error_rate * 100:.1f}%, p95 ≤ {cfg.p95_limit_ms} ms", observed=observed, status=status,
                                    duration_ms=int(m["duration_s"] * 1000), code_ref=t["source"].split(" ", 2)[-1] if ":" in t["source"] else ""))
            ev = [{"type": "requests", "items": [{"method": "GET", "url": url, "status": f"{m['status_codes']}"}]}]
            if erring:
                findings.append(FindingRecord(fingerprint=fingerprint("backend", "performance", "load errors", t["path"]), route="backend", category="performance", severity="high",
                                              title=f"Backend errors under load: GET {t['path']} ({m['error_rate'] * 100:.1f}% of {m['total']} requests failed)",
                                              expected=f"Error rate ≤ {cfg.max_error_rate * 100:.1f}% at {cfg.users} users / {cfg.max_rps} req/s", observed=observed,
                                              standard_ref="ISO/IEC 25010 performance efficiency: capacity", viewport="backend", confidence=0.9, reproduced_n=1, reproduced_m=1,
                                              status=gate_status(0.9), evidence=ev, test_key=key))
            if slow:
                findings.append(FindingRecord(fingerprint=fingerprint("backend", "performance", "load latency", t["path"]), route="backend", category="performance", severity="medium",
                                              title=f"Slow backend under load: GET {t['path']} p95 {m['p95_ms']} ms",
                                              expected=f"p95 ≤ {cfg.p95_limit_ms} ms at {cfg.users} users / {cfg.max_rps} req/s", observed=observed,
                                              standard_ref="ISO/IEC 25010 performance efficiency: time behaviour", viewport="backend", confidence=0.85, reproduced_n=1, reproduced_m=1,
                                              status=gate_status(0.85), evidence=ev, test_key=key))
            events("loadtest", f"GET {t['path']}: {observed[:220]}", None, level="info" if status == "pass" else "warn")
    if summary["endpoints"]:
        summary["status"] = "fail" if any(e["status"] == "fail" for e in summary["endpoints"]) else "pass"
        all_ = summary["endpoints"]
        summary["totals"] = {"requests": sum(e["total"] for e in all_), "failed": sum(e["failed"] for e in all_),
                             "p95_ms_max": max(e["p95_ms"] for e in all_), "rps": round(sum(e["rps"] for e in all_) / len(all_), 2)}
    else:
        summary.update(status="blocked", reason="no endpoint could be load tested (see skipped)")
        tests.append(TestRecord(test_key="load|none", name="Backend API load test", technique="load", category="performance", route="backend",
                                expected="At least one endpoint answers 2xx", observed="; ".join(summary["skipped"][:4]), status="blocked"))
    return summary, tests, findings
