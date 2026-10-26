"""Bounded performance / light-load probe. OFF by default.

This is a light, capped read-only probe to measure how a few endpoints behave
under modest concurrency. It is NOT a stress-to-failure or denial-of-service
test: concurrency, total requests and wall-clock are hard-capped (config, in
turn capped at 50 / 2000 / 120s), it is GET-only against same-origin endpoints
the app itself already called, and it aborts the moment the error rate climbs.
It runs only when explicitly enabled for a target the operator is authorised to
load-test.
"""
from __future__ import annotations

import asyncio
import re
import statistics
from urllib.parse import urlparse

import httpx

from qaplatform.execute.smoke import FindingRecord, TestRecord, fingerprint
from qaplatform.judge.gate import gate_status

CATEGORY = "performance"
ABORT_ERROR_RATE = 0.25  # stop a target the moment a quarter of requests fail


def _targets(pages, api_base: str, limit: int = 6) -> list[str]:
    seen, out = set(), []
    origin = urlparse(api_base).netloc
    for p in pages:
        for r in getattr(p, "api_requests", []) or []:
            if r.get("method") != "GET":
                continue
            path = r.get("path") or ""
            if not path or "login" in path.lower() or "/auth" in path.lower():
                continue
            key = re.sub(r"\d+", "#", path)
            if key in seen:
                continue
            seen.add(key)
            url = r.get("url") or (api_base.rstrip("/") + path)
            if urlparse(url).netloc == origin:
                out.append(url)
        if len(out) >= limit:
            break
    return out[:limit]


async def _probe(url: str, headers: dict, concurrency: int, total: int, max_seconds: float) -> dict:
    lat: list[float] = []
    codes: list[int] = []
    errors = 0
    sent = 0
    loop = asyncio.get_event_loop()
    start = loop.time()
    sem = asyncio.Semaphore(concurrency)
    aborted = False

    async with httpx.AsyncClient(timeout=15, headers=headers, verify=True) as client:
        async def one():
            nonlocal errors
            async with sem:
                t0 = loop.time()
                try:
                    r = await client.get(url)
                    lat.append((loop.time() - t0) * 1000)
                    codes.append(r.status_code)
                    if r.status_code >= 500:
                        errors += 1
                except Exception:
                    errors += 1
                    lat.append((loop.time() - t0) * 1000)

        tasks: set = set()
        while sent < total and (loop.time() - start) < max_seconds:
            while len(tasks) < concurrency and sent < total:
                tasks.add(asyncio.create_task(one()))
                sent += 1
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if sent >= concurrency and errors / max(sent, 1) > ABORT_ERROR_RATE:
                aborted = True
                break
        if tasks:
            await asyncio.wait(tasks)

    lat.sort()
    def pct(p):
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else 0.0
    return {"url": url, "sent": sent, "errors": errors, "error_rate": round(errors / max(sent, 1), 3),
            "p50": pct(0.5), "p95": pct(0.95), "max": round(max(lat), 1) if lat else 0.0,
            "elapsed_s": round(loop.time() - start, 1), "aborted": aborted,
            "concurrency": concurrency, "status_sample": sorted(set(codes))[:6]}


def run_load(settings, live_url: str, pages, api_base: str, auth_headers: dict | None, events) -> tuple[list[TestRecord], list[FindingRecord]]:
    tests: list[TestRecord] = []
    findings: list[FindingRecord] = []
    targets = _targets(pages, api_base)
    if not targets:
        return tests, findings
    conc = int(settings.load_max_concurrency)
    per_target = max(1, int(settings.load_max_requests) // max(len(targets), 1))
    max_s = max(5, int(settings.load_max_seconds) // max(len(targets), 1))
    events("load", f"Light load probe: {len(targets)} endpoint(s), ≤{conc} concurrent, ≤{per_target} requests each, read-only", 91)

    for url in targets:
        path = urlparse(url).path
        try:
            res = asyncio.run(_probe(url, auth_headers or {}, conc, per_target, max_s))
        except Exception as exc:
            tests.append(TestRecord(test_key=f"load|{path}", name=f"Light load on {path}", technique="load", category=CATEGORY, route=path,
                                    expected="Endpoint stays responsive under modest concurrency", observed=f"probe error: {type(exc).__name__}", status="blocked"))
            continue
        healthy = res["error_rate"] <= 0.02 and not res["aborted"]
        t = TestRecord(test_key=f"load|{path}", name=f"Light load on {path} ({conc} concurrent)", technique="load", category=CATEGORY, route=path,
                       steps=[{"type": "LOAD", "value": f"{res['sent']} GET requests, {conc} concurrent, {res['elapsed_s']}s"}],
                       expected=f"Under {conc} concurrent readers the endpoint stays responsive (p95 < 2000 ms) with no 5xx",
                       observed=f"sent {res['sent']}, error rate {res['error_rate']}, p50 {res['p50']}ms, p95 {res['p95']}ms, max {res['max']}ms, statuses {res['status_sample']}" + (" (ABORTED on errors)" if res["aborted"] else ""),
                       status="pass" if healthy and res["p95"] < 2000 else "fail", reproduced_n=1, reproduced_m=1)
        tests.append(t)
        if res["aborted"] or res["error_rate"] > 0.05:
            findings.append(FindingRecord(fingerprint=fingerprint("load-err", path), route=path, category=CATEGORY, severity="high",
                                          title=f"{path} returns errors under light load ({conc} concurrent)", expected=t.expected, observed=t.observed,
                                          standard_ref="ISO/IEC 25010 Performance efficiency; Reliability", confidence=0.85, reproduced_n=1, reproduced_m=1, status=gate_status(0.85)))
        elif res["p95"] >= 2000:
            findings.append(FindingRecord(fingerprint=fingerprint("load-slow", path), route=path, category=CATEGORY, severity="low",
                                          title=f"{path} is slow under light load (p95 {res['p95']}ms)", expected=t.expected, observed=t.observed,
                                          standard_ref="ISO/IEC 25010 Performance efficiency (time behaviour)", confidence=0.8, reproduced_n=1, reproduced_m=1, status=gate_status(0.8)))
    return tests, findings
