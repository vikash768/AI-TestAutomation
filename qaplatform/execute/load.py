"""Staged load test with closed-loop virtual users. OFF by default.

Runs only when the operator enabled it and confirmed they are authorised to load-test the target.
Virtual users replay the GET reads the app itself made (plus the page document) round-robin with
think time, ramping 1 -> N users in stages, so a slowdown can be tied to a concurrency level.

Safety is not optional: GET only, same-origin only, users/seconds/requests are hard-capped by
settings (themselves capped at 50 / 120 s / 2000), nothing is ever retried, and the whole test
stops the moment the recent server-error rate passes 25% (or the site is mostly answering 429).
Auth headers stay in memory and only go to the app's own hosts; no header value or query string
is ever written to tests, findings, stats or events.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import math
import random
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlsplit

import httpx

from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.judge.gate import gate_status

CATEGORY = "performance"
MAX_TARGETS = 8
THINK_S = (0.1, 0.3)
ABORT_SERVER_ERROR_RATE = 0.25
REJECT_SHARE = 0.05      # more than 5% 401/403 or 429 answers: the endpoint's numbers are not a clean measurement
STOP_RATE_LIMITED = 0.6
WINDOW_MIN = 20          # never judge an abort on fewer samples: a 1-user stage is too noisy
FINDING_ERROR_RATE = 0.05
OVERALL_RATE_LIMITED = 0.10
SKIP_RE = re.compile(r"(^|[/_.-])(log-?in|log-?out|sign-?in|sign-?out|sign-?up|register|auth|oauth2?|token|refresh|session|sso|password|"
                     r"export|download|stream|events|sse|socket|ws)([/_.-]|$)", re.I)
ID_SEG_RE = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,}|(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{16,})$", re.I)
LOGIN_LOC_RE = re.compile(r"(log-?in|sign-?in|auth)", re.I)
LATENCY_KINDS = ("ok", "server", "client")  # 401/403/429 answers are rejections, not service time


@dataclass
class Target:
    path: str     # id-collapsed path, the only form that is ever reported
    url: str
    kind: str     # "page" | "api"
    auth: bool


@dataclass
class _Sample:
    stage: int
    target: int
    status: int   # 0 = timeout / connection error
    ms: float
    kind: str     # ok | server | auth | limited | client


@dataclass
class _State:
    max_requests: int
    window: deque
    sent: int = 0
    samples: list = field(default_factory=list)
    stage_s: list = field(default_factory=list)
    stop: bool = False
    abort_kind: str = ""
    abort_reason: str = ""
    budget_exhausted: bool = False
    cut: int = 0             # requests cut by our own stage deadline (not samples)


def collapse(path: str) -> str:
    return "/".join(":id" if ID_SEG_RE.match(s) else s for s in (path or "/").split("/")) or "/"


def _targets(live_url: str, pages, api_base: str | None, limit: int = MAX_TARGETS) -> list[Target]:
    live = urlsplit(live_url)
    hosts = {live.netloc, urlsplit(api_base or "").netloc} - {""}
    doc = urldefrag(live_url)[0]
    out = [Target(live.path or "/", doc if live.path else doc.rstrip("/") + "/", "page", False)]
    seen = {out[0].path}
    for p in pages or []:
        for r in getattr(p, "api_requests", None) or []:
            if len(out) >= limit:
                return out
            if str(r.get("method") or "").upper() != "GET":
                continue
            url = urldefrag(r.get("url") or ((api_base or live_url).rstrip("/") + (r.get("path") or "")))[0]
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or u.netloc not in hosts or SKIP_RE.search(u.path or "/"):
                continue
            key = collapse(u.path or "/")
            if key in seen:
                continue
            seen.add(key)
            out.append(Target(key, url, "api", True))
    return out


CAPACITY_STEPS = (1, 2, 5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200, 300, 400, 500)
BREAK_ERROR_RATE = 0.05     # a step "fails" when more than 5% of its answers are 5xx/timeouts
THROUGHPUT_DROP = 0.90      # ...or when more users get >10% LESS throughput (overloaded)


def _capacity_plan(max_users: int) -> list[int]:
    steps = [n for n in CAPACITY_STEPS if n < max_users]
    return steps + [max_users]


def _step_breach(stages: list[dict], p95_ms: int) -> str:
    """Why the last measured step counts as broken, or '' if it held."""
    cur = stages[-1]
    if cur["requests"] and cur["error_rate"] > BREAK_ERROR_RATE:
        return f"{cur['error_rate']:.0%} of requests failed (5xx/timeouts)"
    if cur["p95"] is not None and cur["p95"] > p95_ms:
        return f"p95 {cur['p95']:.0f} ms exceeded the {p95_ms} ms target"
    if len(stages) >= 2:
        prev = stages[-2]
        # More users but LESS work done: the server is overloaded, whatever the step size.
        if prev["rps"] and cur["users"] > prev["users"] and cur["rps"] < prev["rps"] * THROUGHPUT_DROP:
            return (f"throughput fell {cur['rps'] / prev['rps'] - 1:.0%} ({prev['rps']:.0f} → {cur['rps']:.0f} req/s) while users rose "
                    f"{prev['users']} → {cur['users']}" + (f"; p95 {prev['p95']:.0f} → {cur['p95']:.0f} ms" if prev["p95"] and cur["p95"] else ""))
    return ""


def knee_of(stages: list[dict]) -> dict | None:
    """Where the site stops keeping up: the first step whose throughput grows by less than half
    the user increase while p95 reaches 3x the 1-user p95 (and >500 ms). Independent of the target."""
    measured = [x for x in stages if x["requests"]]
    if len(measured) < 2 or not measured[0]["p95"]:
        return None
    base = measured[0]["p95"]
    for prev, cur in zip(measured, measured[1:]):
        if not (prev["rps"] and cur["p95"] and cur["users"] > prev["users"]):
            continue
        rps_gain, users_gain = cur["rps"] / prev["rps"] - 1, cur["users"] / prev["users"] - 1
        if rps_gain < 0.5 * users_gain and cur["p95"] >= 3 * base and cur["p95"] > 500:
            return {"users": cur["users"], "p50": cur["p50"], "p95": cur["p95"], "rps": cur["rps"],
                    "reason": f"+{users_gain:.0%} users gave only {rps_gain:+.0%} throughput while p95 rose to {cur['p95']:.0f} ms ({cur['p95'] / base:.0f}x the 1-user p95)"}
    return None


def capacity_text(cap: dict, verdict: str, abort_reason: str = "") -> str:
    bp, kn, ok = cap.get("breaking_point"), cap.get("knee"), cap.get("last_healthy")
    if verdict == "not_measurable":
        return "not measurable: the API endpoints could not be reached with a valid session"
    parts = []
    if ok:
        parts.append(f"healthy up to {ok['users']} users (p50 {ok['p50']} ms, p95 {ok['p95']} ms, {ok['rps']} req/s)")
    if kn and (not bp or kn["users"] < bp["users"]):
        parts.append(f"slows down from {kn['users']} users: {kn['reason']} (p50 {kn['p50']} ms, p95 {kn['p95']} ms)")
    if bp:
        parts.append(f"breaks at {bp['users']} users: {bp['reason']} (p50 {bp['p50']} ms, p95 {bp['p95']} ms, {bp['rps']} req/s)")
    elif not kn:
        parts.append(f"no breaking point up to {cap['max_tested_users']} users (the configured maximum)")
    else:
        parts.append(f"did not break outright up to {cap['max_tested_users']} users")
    if cap.get("stopped_early") and abort_reason:
        parts.append(f"stopped early: {abort_reason}")
    return "; ".join(parts)


def capacity_summary(stages: list[dict], p95_ms: int, step_seconds: float, expected_users: int = 0, stopped_early: bool = False) -> dict:
    """Breaking point, knee and last healthy step, from the measured steps (also used to re-evaluate a stored run)."""
    measured = [x for x in stages if x["requests"]]
    broken = next((cur for i, cur in enumerate(measured) if _step_breach(measured[: i + 1], p95_ms)), None)
    knee = knee_of(measured)
    limit = min(x["users"] for x in (broken, knee) if x) if (broken or knee) else None
    healthy = [x for x in measured if limit is None or x["users"] < limit]
    ok = healthy[-1] if healthy else None
    pick = lambda x: {"users": x["users"], "p50": x["p50"], "p95": x["p95"], "rps": x["rps"]}
    return {"max_tested_users": measured[-1]["users"] if measured else 0, "step_seconds": step_seconds, "p95_target_ms": p95_ms,
            "breaking_point": ({**pick(broken), "error_rate": broken["error_rate"], "reason": _step_breach(measured[: measured.index(broken) + 1], p95_ms)} if broken else None),
            "knee": knee, "last_healthy": pick(ok) if ok else None, "stopped_early": stopped_early, "expected_users": int(expected_users or 0)}


def _plan(users: int) -> list[int]:
    out: list[int] = []
    for n in (1, math.ceil(users / 4), math.ceil(users / 2), users):
        if n >= 1 and n not in out:
            out.append(n)
    return out


def _classify(status: int, location: str = "") -> str:
    if status == 0 or status >= 500:
        return "server"
    if status == 429:
        return "limited"
    if status in (401, 403) or (300 <= status < 400 and LOGIN_LOC_RE.search(location or "")):
        return "auth"
    return "ok" if status < 400 else "client"


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[max(0, math.ceil(p * len(s)) - 1)], 1)


async def _engine(targets: list[Target], headers: dict, plan: list[int], stage_s: float, cap: int, st: _State, timeout: float, events, after_step=None) -> None:
    loop = asyncio.get_running_loop()
    stop_evt = asyncio.Event()
    limits = httpx.Limits(max_connections=cap, max_keepalive_connections=cap)

    def check(n_users: int) -> None:
        w = list(st.window)[-max(WINDOW_MIN, 2 * n_users):]
        if len(w) < WINDOW_MIN or st.stop:
            return
        se, rl = sum(k == "server" for k in w) / len(w), sum(k == "limited" for k in w) / len(w)
        if se > ABORT_SERVER_ERROR_RATE:
            st.abort_kind, st.abort_reason = "server_errors", f"server-error rate {se:.0%} over the last {len(w)} requests at {n_users} users exceeded {ABORT_SERVER_ERROR_RATE:.0%}"
        elif rl >= STOP_RATE_LIMITED:
            st.abort_kind, st.abort_reason = "rate_limited", f"site answered 429 to {rl:.0%} of the last {len(w)} requests at {n_users} users; stopped to respect its rate limit"
        if st.abort_kind:
            st.stop = True
            stop_evt.set()

    no_cookies = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False, cookies=no_cookies) as client:
        async def user(si: int, n_users: int, i: int, deadline: float) -> None:
            k = i % len(targets)
            while not st.stop and loop.time() < deadline:
                if st.sent >= st.max_requests:
                    st.budget_exhausted = True
                    return
                st.sent += 1  # reserved before the await so concurrent users can never overshoot the cap
                ti, k = k, (k + 1) % len(targets)
                t = targets[ti]
                t0 = loop.time()
                budget = min(timeout, max(0.5, deadline - loop.time()))
                try:
                    # never let one request outlive the stage: the time cap must hold even when the server slows
                    r = await client.get(t.url, headers=headers if (t.auth and headers) else None, timeout=budget)
                    status, loc = r.status_code, r.headers.get("location", "")
                except httpx.TimeoutException:
                    if budget < timeout:
                        st.cut += 1  # cut by OUR stage deadline, not a server timeout: not a sample
                        return
                    status, loc = 0, ""
                except Exception:
                    status, loc = 0, ""
                client.cookies.clear()  # a Set-Cookie must not turn the public page target into a signed-in one
                kind = _classify(status, loc)
                st.samples.append(_Sample(si, ti, status, (loop.time() - t0) * 1000, kind))
                st.window.append(kind)
                check(n_users)
                left = deadline - loop.time()
                if st.stop or left <= 0:
                    return
                try:
                    await asyncio.wait_for(stop_evt.wait(), min(random.uniform(*THINK_S), left))
                except asyncio.TimeoutError:
                    pass

        for si, n in enumerate(plan):
            if st.stop or st.budget_exhausted:
                break
            t0 = loop.time()
            await asyncio.gather(*(user(si, n, i, t0 + stage_s) for i in range(n)))
            st.stage_s.append(loop.time() - t0)
            s = [x for x in st.samples if x.stage == si]
            p95 = _pct([x.ms for x in s if x.kind in LATENCY_KINDS], 0.95)
            events("load", f"Load stage {si + 1}/{len(plan)}: {n} users, {len(s) / max(st.stage_s[-1], 1e-6):.1f} rps, "
                           f"p95 {'n/a' if p95 is None else f'{p95:.0f}ms'}" + (f" (stopped: {st.abort_reason})" if st.stop else ""), 91 + min(si, 4))
            if after_step and not st.stop and after_step(si):
                st.stop = True
                stop_evt.set()


def _arun(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as ex:  # called from inside an event loop: give the test its own
        return ex.submit(asyncio.run, coro).result()


def _endpoint(ti: int, t: Target, samples: list[_Sample], plan: list[int], p95_ms: int) -> dict:
    s = [x for x in samples if x.target == ti]
    n = len(s)
    c = {k: sum(x.kind == k for x in s) for k in ("ok", "server", "auth", "limited", "client")}
    timeouts = sum(x.status == 0 for x in s)
    lat = [x.ms for x in s if x.kind in LATENCY_KINDS]
    ok_by_stage = {si: [x.ms for x in s if x.stage == si and x.kind == "ok"] for si in range(len(plan))}
    base = _pct(ok_by_stage.get(0, []), 0.95)
    peak_si = max((si for si, v in ok_by_stage.items() if si > 0 and len(v) >= 3), default=None)
    peak = _pct(ok_by_stage[peak_si], 0.95) if peak_si is not None else None
    blocked = ""
    if not n:
        blocked = "not reached before the request budget or time ran out"
    elif c["auth"] / n > REJECT_SHARE:
        blocked = f"{c['auth']}/{n} answers were 401/403 (or a redirect to sign-in): the load test had no valid session for it"
    elif c["limited"] / n > REJECT_SHARE:
        blocked = f"{c['limited']}/{n} answers were 429: the site rate-limited this endpoint"
    elif c["ok"] / n < 0.5 and c["server"] / n <= FINDING_ERROR_RATE:
        blocked = f"only {c['ok']}/{n} answers were 2xx/3xx (mostly {c['client']} other 4xx)"
    first_err = next((plan[si] for si in range(len(plan)) if (ss := [x for x in s if x.stage == si]) and sum(x.kind == "server" for x in ss) / len(ss) > FINDING_ERROR_RATE), None)
    slow = slow_flat = False
    if peak is not None and peak_si is not None:
        hot = peak > p95_ms or (base is not None and base > 0 and peak / base >= 3 and peak > 500)
        slow = hot and (base is None or peak >= 1.5 * base)
        slow_flat = hot and not slow and base is not None and base > p95_ms
    return {"path": t.path, "kind": t.kind, "requests": n, "ok": c["ok"], "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95),
            "server_errors": c["server"], "timeouts": timeouts, "client_errors": c["auth"] + c["client"], "auth_errors": c["auth"],
            "rate_limited": c["limited"], "statuses": sorted({x.status for x in s}), "measurable": bool(n) and not blocked and c["ok"] / n >= 0.5,
            "blocked_reason": blocked, "base_p95": base, "peak_p95": peak, "peak_users": plan[peak_si] if peak_si is not None else None,
            "first_error_users": first_err, "slow": slow, "slow_flat": slow_flat}


def run_load(settings, live_url: str, pages, api_base: str, auth_headers: dict, events, users: int = 10, seconds: int = 60,
             p95_ms: int = 2000, auth_kind: str = "none", mode: str = "ramp", step_seconds: int = 15,
             expected_users: int = 0) -> tuple[list[TestRecord], list[FindingRecord], dict]:
    capacity = mode == "capacity"
    if capacity:
        # Breaking-point search: its own, larger operator-set caps (CAPACITY_* in .env).
        cap_u = max(1, min(int(getattr(settings, "capacity_max_users", 50)), 500))
        cap_r = max(1, min(int(getattr(settings, "capacity_max_requests", 20000)), 100000))
        step_seconds = max(5, min(int(step_seconds or 15), 60))
        users = max(1, min(int(users or cap_u), cap_u))
        plan = _capacity_plan(users)
        stage_s = float(step_seconds)
        seconds = int(stage_s * len(plan))
        cap_s = seconds
    else:
        cap_u = max(1, min(int(settings.load_max_concurrency), 50))
        cap_r = max(1, min(int(settings.load_max_requests), 2000))
        cap_s = max(1, min(int(settings.load_max_seconds), 120))
        users, seconds = max(1, min(int(users or 1), cap_u)), max(1, min(int(seconds or 1), cap_s))
        plan = _plan(users)
        stage_s = seconds / len(plan)
    p95_ms = max(1, int(p95_ms or 2000))
    headers = {str(k): str(v) for k, v in (auth_headers or {}).items() if v}
    targets = _targets(live_url, pages, api_base)
    timeout = min(15.0, max(5.0, p95_ms * 3 / 1000))
    events("load", (f"Capacity test: stepping {' → '.join(map(str, plan))} users, {stage_s:.0f}s per step, stops at the first failing step; "
                    if capacity else f"Load test: ramp {' → '.join(map(str, plan))} users over {seconds}s; ")
                   + f"{len(targets)} target(s) ({', '.join(t.path for t in targets)}), ≤{cap_r} requests, GET only, credentials: {auth_kind}", 91)
    st = _State(max_requests=cap_r, window=deque(maxlen=max(WINDOW_MIN, 2 * cap_u)))
    breach: dict = {}

    def stage_stats(si: int) -> dict:
        s = [x for x in st.samples if x.stage == si]
        lat = [x.ms for x in s if x.kind in LATENCY_KINDS]
        se = sum(x.kind == "server" for x in s)
        secs = st.stage_s[si] if si < len(st.stage_s) else stage_s
        return {"users": plan[si], "seconds": round(secs, 2), "requests": len(s), "rps": round(len(s) / max(secs, 1e-6), 2),
                "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99), "server_errors": se,
                "client_errors": sum(x.kind in ("auth", "client") for x in s), "rate_limited": sum(x.kind == "limited" for x in s),
                "error_rate": round(se / len(s), 3) if s else 0.0}

    def step_check(si: int) -> bool:
        why = _step_breach([stage_stats(i) for i in range(si + 1)], p95_ms)
        if why:
            breach.update(users=plan[si], reason=why)
            events("load", f"Breaking point: {plan[si]} users — {why}; stopping here so the site is not pushed further", None)
        return bool(why)
    t_start = time.time()
    async def bounded():
        try:
            await asyncio.wait_for(_engine(targets, headers, plan, stage_s, cap_u, st, timeout, events, after_step=step_check if capacity else None), timeout=cap_s + 5)
        except asyncio.TimeoutError:
            st.stop = True
            st.abort_kind = st.abort_kind or "time_cap"
            st.abort_reason = st.abort_reason or f"stopped at the {cap_s}s time cap"
    _arun(bounded())
    duration_ms = int((time.time() - t_start) * 1000)

    samples = st.samples
    stages = [stage_stats(si) for si in range(len(st.stage_s))]
    eps = [_endpoint(i, t, samples, plan, p95_ms) for i, t in enumerate(targets)]

    total = len(samples)
    server_rate = sum(x.kind == "server" for x in samples) / total if total else 0.0
    limited_rate = sum(x.kind == "limited" for x in samples) / total if total else 0.0
    base_p95 = stages[0]["p95"] if stages else None
    peak = next((s for s in reversed(stages) if s["p95"] is not None and s["requests"] >= 5), None)
    knee_limit = max(p95_ms, 3 * base_p95) if base_p95 else p95_ms
    knee = next((s["users"] for s in stages if s["p95"] is not None and s["p95"] > knee_limit), None)
    degraded = bool(peak and (peak["p95"] > p95_ms or (base_p95 and peak["p95"] / base_p95 >= 3 and peak["p95"] > 500))) \
        or any(e["slow"] or e["slow_flat"] for e in eps) or limited_rate >= OVERALL_RATE_LIMITED
    judged = [e for e in eps if e["kind"] == "api"]  # a static page document alone says nothing about the app
    if st.abort_kind == "server_errors" or server_rate > FINDING_ERROR_RATE:
        verdict = "fail"
    elif not any(e["measurable"] for e in judged):
        verdict = "not_measurable"
        st.abort_reason = st.abort_reason or ("no API endpoints were captured from the app" if not judged else "")
    else:
        verdict = "degraded" if degraded else "pass"

    stats = {"auth": auth_kind, "users": users, "seconds": seconds, "p95_target_ms": p95_ms, "aborted": bool(st.abort_kind),
             "abort_reason": st.abort_reason, "abort_kind": st.abort_kind, "budget_exhausted": st.budget_exhausted, "max_requests": cap_r,
             "requests": total, "stages": stages,
             "endpoints": [{k: e[k] for k in ("path", "kind", "requests", "ok", "p50", "p95", "server_errors", "timeouts", "client_errors",
                                              "auth_errors", "rate_limited", "statuses", "measurable", "blocked_reason")} for e in eps],
             "verdict": verdict, "knee_users": knee, "mode": mode}
    if capacity:
        stats["capacity"] = capacity_summary(stages, p95_ms, stage_s, expected_users, stopped_early=bool(st.abort_kind) and not breach)

    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    expected = (f"Under a ramp of 1 → {users} concurrent readers the endpoint answers 2xx with no 5xx/timeouts and p95 under {p95_ms} ms, "
                f"without slowing to 3x its single-user p95")
    for ei, e in enumerate(eps):
        lat = f"p50 {e['p50']} ms, p95 {e['p95']} ms" + (f" (1 user: {e['base_p95']} ms, {e['peak_users']} users: {e['peak_p95']} ms)" if e["peak_p95"] is not None else "")
        observed = (f"{e['requests']} requests: {e['ok']} ok, {e['server_errors'] - e['timeouts']} 5xx, {e['timeouts']} timeouts/connection errors, "
                    f"{e['auth_errors']} 401/403, {e['rate_limited']} 429, {e['client_errors'] - e['auth_errors']} other 4xx; {lat}; statuses {e['statuses']}")
        if e["blocked_reason"]:
            observed += f". Not measurable: {e['blocked_reason']}"
        failed = e["requests"] and (e["server_errors"] / e["requests"] > FINDING_ERROR_RATE or e["slow"] or e["slow_flat"])
        status = "fail" if failed else ("blocked" if e["blocked_reason"] else "pass")
        key = f"load|{e['path']}"
        tests.append(TestRecord(test_key=key, name=f"Load on {e['path']} (1 → {users} users)", technique="load", category=CATEGORY, route=e["path"],
                                steps=[{"type": "LOAD", "value": f"{e['requests']} GET requests during a 1 → {users} user ramp"}],
                                expected=expected, observed=observed, status=status, duration_ms=duration_ms, reproduced_n=1, reproduced_m=1))
        n, se = e["requests"], e["server_errors"]
        if n >= 5 and se >= 3 and se / n > FINDING_ERROR_RATE:
            conf = 0.9 if se >= 5 else 0.75
            at = e["first_error_users"] or users
            findings.append(FindingRecord(
                fingerprint=fingerprint("load-5xx", e["path"]), route=e["path"], category=CATEGORY, severity="high",
                title=f"{e['path']} returns server errors under load ({at} users)",
                description=f"{se} of {n} GET requests failed with 5xx or a timeout; errors first passed {FINDING_ERROR_RATE:.0%} at {at} concurrent users"
                            + (" (already at 1 user, so not only load-related)" if at == 1 else "") + ".",
                expected=expected, observed=observed, standard_ref="ISO/IEC 25010 Reliability (fault tolerance); Performance efficiency (capacity)",
                confidence=conf, reproduced_n=1, reproduced_m=1, status=gate_status(conf), test_key=key))
        elif e["slow"]:
            conf = 0.85 if sum(1 for x in samples if x.target == ei and x.kind == "ok" and x.stage > 0) >= 10 else 0.7
            vs = f" vs {e['base_p95']:.0f}ms at 1" if e["base_p95"] is not None else ""
            findings.append(FindingRecord(
                fingerprint=fingerprint("load-slow", e["path"]), route=e["path"], category=CATEGORY, severity="medium",
                title=f"{e['path']} slows down under load (p95 {e['peak_p95']:.0f}ms at {e['peak_users']} users{vs})",
                description=f"Response time grows with concurrency (target p95 {p95_ms} ms).", expected=expected, observed=observed,
                standard_ref="ISO/IEC 25010 Performance efficiency (time behaviour, capacity)", confidence=conf, reproduced_n=1, reproduced_m=1,
                status=gate_status(conf), test_key=key))
        elif e["slow_flat"]:
            findings.append(FindingRecord(
                fingerprint=fingerprint("load-slow-flat", e["path"]), route=e["path"], category=CATEGORY, severity="low",
                title=f"{e['path']} is slow even at 1 user (p95 {e['base_p95']:.0f}ms)",
                description=f"Already above the {p95_ms} ms p95 target without load; it does not get much worse as users grow.",
                expected=expected, observed=observed, standard_ref="ISO/IEC 25010 Performance efficiency (time behaviour)",
                confidence=0.8, reproduced_n=1, reproduced_m=1, status=gate_status(0.8), test_key=key))

    limited = [(i, e) for i, e in enumerate(eps) if e["requests"] and e["rate_limited"] / e["requests"] >= 0.5]
    if limited:
        at = next((plan[si] for si in range(len(st.stage_s)) for i, _ in limited
                   if (ss := [x for x in samples if x.stage == si and x.target == i]) and sum(x.kind == "limited" for x in ss) / len(ss) >= 0.5), users)
        conf = 0.6  # a rate limit may be intended protection, so a human decides
        findings.append(FindingRecord(
            fingerprint=fingerprint("load-429", urlsplit(live_url).netloc), route=limited[0][1]["path"], category=CATEGORY, severity="medium",
            title=f"Site rate-limits normal read traffic at {at} users",
            description=f"Mostly 429 on {', '.join(e['path'] for _, e in limited)} during ordinary GET reads with think time. "
                        "This may be intended protection; it also means real users browsing together can be refused.",
            expected=expected, observed="; ".join(f"{e['path']}: {e['rate_limited']}/{e['requests']} answers were 429" for _, e in limited),
            standard_ref="ISO/IEC 25010 Performance efficiency (capacity)", confidence=conf, reproduced_n=1, reproduced_m=1,
            status=gate_status(conf), test_key=f"load|{limited[0][1]['path']}"))

    ramp = "; ".join(f"{s['users']} users: {s['requests']} req, {s['rps']} rps, p95 {s['p95']} ms, {s['server_errors']} server errors, "
                     f"{s['client_errors']} 4xx, {s['rate_limited']} 429" for s in stages)
    summary = f"verdict {verdict}" + (f", knee at {knee} users" if knee else "") + f". {ramp}"
    if st.abort_reason:
        summary += f". STOPPED: {st.abort_reason}"
    if st.budget_exhausted:
        summary += f". Request budget ({cap_r}) reached"
    tests.append(TestRecord(test_key="load|ramp", name=f"Load ramp 1→{users} users", technique="load", category=CATEGORY, route=targets[0].path,
                            steps=[{"type": "LOAD", "value": f"stages {plan}, {seconds}s, ≤{cap_r} requests, {len(targets)} targets, credentials: {auth_kind}"}],
                            expected=f"Ramp to {users} users with no server errors and p95 under {p95_ms} ms", observed=summary,
                            status={"pass": "pass", "not_measurable": "blocked"}.get(verdict, "fail"), duration_ms=duration_ms,
                            reproduced_n=1, reproduced_m=1))
    events("load", f"Load test {verdict}: {total} requests" + (f", knee at {knee} users" if knee else "") + (f", stopped: {st.abort_reason}" if st.abort_reason else ""), 95)
    if capacity:
        # Pushing to the breaking point makes the last step slow or failing on purpose: that is the
        # measurement, not a defect. It is reported once, as capacity (a finding only below the
        # expected user count); only the rate-limit flag stays.
        findings = [f for f in findings if "rate-limits" in f.title]
        cap = stats["capacity"]
        if stats["verdict"] != "not_measurable":
            stats["verdict"] = "fail" if (cap["breaking_point"] and cap["expected_users"] and cap["breaking_point"]["users"] <= cap["expected_users"]) else "pass"
        stats["knee_users"] = (cap.get("knee") or {}).get("users")
        summary = capacity_text(cap, stats["verdict"], st.abort_reason)
        bp = cap["breaking_point"]
        want = cap["expected_users"]
        below = bool(bp and want and bp["users"] <= want)
        tests.append(TestRecord(test_key="load|capacity", name=f"Capacity: users the site serves within p95 {p95_ms} ms and <{BREAK_ERROR_RATE:.0%} errors",
                                technique="load", category=CATEGORY, route="/",
                                steps=[{"type": "LOAD", "value": f"step {' → '.join(str(x['users']) for x in stages)} users, {stage_s:.0f}s per step, stop at the first failing step"}],
                                expected=(f"Serves {want} concurrent users" if want else "Find the breaking point") + f" within p95 {p95_ms} ms and <{BREAK_ERROR_RATE:.0%} errors",
                                observed=summary, status="blocked" if stats["verdict"] == "not_measurable" else ("fail" if below else "pass"),
                                duration_ms=duration_ms, reproduced_n=1, reproduced_m=1))
        if below:
            findings.append(FindingRecord(fingerprint=fingerprint("load-capacity", str(want)), route="/", category=CATEGORY, severity="high",
                                          title=f"Site breaks at {bp['users']} concurrent users, below the expected {want}",
                                          expected=tests[-1].expected, observed=summary, standard_ref="ISO/IEC 25010 Performance efficiency (capacity)",
                                          confidence=0.85, reproduced_n=1, reproduced_m=1, status=gate_status(0.85)))
    return tests, findings, stats
