"""Self-contained HTML report written into the run folder.

The report is served at /runs/{run_id}/report while the run's files are served
under /artifacts/, so every evidence link is root-relative
(/artifacts/runs/{run_id}/...), checked to stay inside the run folder, and only
emitted when the file exists.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote

from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedPage

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
NATIVE = ("android", "ios", "macos")
DEVICE_PERF_TECHNIQUES = ("launch", "performance")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def artifact_href(path: str, run_dir: Path, run_id: str) -> str:
    """URL of a file in this run's folder, or '' when it is missing or outside the folder (no traversal)."""
    if not path:
        return ""
    p = Path(path)
    # a relative path is relative to the working directory (artifacts/runs/<id>/...) or to the run folder (screenshots/...)
    for cand in ([p] if p.is_absolute() else [Path.cwd() / p, run_dir / p]):
        try:
            rel = cand.resolve().relative_to(run_dir.resolve())
        except (ValueError, OSError):
            continue
        if rel.parts and (run_dir / rel).is_file():
            return f"/artifacts/runs/{quote(run_id, safe='')}/{quote(rel.as_posix())}"
    return ""


def _link(path: str, run_dir: Path, run_id: str, text: str) -> str:
    href = artifact_href(path, run_dir, run_id)
    return f'<a href="{_e(href)}" target="_blank">{_e(text)}</a>' if href else ""


def _evidence(items: list, run_dir: Path, run_id: str) -> str:
    ev = []
    for item in items or []:
        if item.get("type") == "screenshot" and item.get("path"):
            ev.append(_link(item["path"], run_dir, run_id, "screenshot" + ((" " + item["viewport"]) if item.get("viewport") else "")) or "<small>screenshot missing</small>")
        elif item.get("type") == "console":
            ev.append("<details><summary>console</summary><pre>" + _e("\n".join(item.get("messages", []))) + "</pre></details>")
        elif item.get("type") == "requests":
            ev.append("<details><summary>requests</summary><pre>" + _e("\n".join(f"{r.get('method')} {r.get('url')} -> {r.get('status') or r.get('error')}" for r in item.get("items", []))) + "</pre></details>")
        elif item.get("type") == "layout":
            ev.append("<details><summary>layout</summary><pre>" + _e(json.dumps({k: v for k, v in item.items() if k != "type"}, indent=1)[:2000]) + "</pre></details>")
    return " ".join(x for x in ev if x)


def _stat(value, label: str) -> str:
    return f'<div class="stat"><b>{_e(value)}</b>{_e(label)}</div>'


def _steps(steps: list) -> str:
    out = []
    for s in steps or []:
        if isinstance(s, dict):
            text = s.get("text") or " ".join(str(s.get(k)) for k in ("action", "target", "value") if s.get(k))
            if s.get("result"):
                text += f" → {s['result']}"
            out.append(text)
        else:
            out.append(str(s))
    return "<ol>" + "".join(f"<li>{_e(x)}</li>" for x in out[:25]) + "</ol>" if out else ""


def _native_summary(stats: dict, pages: list, tests: list[TestRecord], uniq: list[FindingRecord]) -> str:
    by = stats.get("tests_by_status") or {}
    qa = stats.get("qa_data") or {}
    lt = stats.get("load_test") or {}
    j = stats.get("journeys") or {}
    return '<div class="stats">' + "".join([
        _stat(stats.get("code_interactive", 0), "code components"),
        _stat(stats.get("matched_interaction", stats.get("matched", 0)), "live components found"),
        _stat(f"{stats.get('coverage_interaction', stats.get('coverage', 0)) * 100:.0f}%", "coverage of code components"),
        _stat(f"{stats.get('tested_components', 0)} ({stats.get('tested_ratio', 0) * 100:.0f}%)", "components exercised"),
        _stat(stats.get("screens", len(pages)), "screens explored"),
        _stat(len(tests), "tests executed"),
        _stat(by.get("pass", 0), "pass"),
        _stat(by.get("fail", 0) + by.get("flaky", 0), "fail"),
        _stat(by.get("blocked", 0), "blocked"),
        _stat(by.get("needs_review", 0) + sum(1 for f in uniq if f.status != "reported"), "needs review"),
        _stat(f"{j.get('completed', 0)}/{j.get('total', 0)}", "full journeys completed"),
        _stat(stats.get("safe_actions", stats.get("taps", 0)), "safe actions executed"),
        _stat(stats.get("held_back", 0), "actions held by safety guard"),
        _stat(qa.get("created", 0), "QA records created"),
        _stat(f"{qa.get('cleaned', 0)}" + (f" ({qa.get('leftover', 0)} left)" if qa.get("leftover") else ""), "QA records cleaned"),
        _stat(lt.get("status", "not run"), "load test"),
        _stat(sum(1 for f in uniq if f.status == "reported"), "findings reported"),
    ]) + "</div>"


def _perf_section(stats: dict, tests: list[TestRecord]) -> str:
    device = [t for t in tests if t.technique in DEVICE_PERF_TECHNIQUES]
    drows = "".join(f"<tr class='t-{_e(t.status)}'><td>{_e(t.name)}</td><td>{_e(t.expected)}</td><td>{_e(t.observed)}</td><td>{_e(t.status)}</td></tr>" for t in device)
    lt = stats.get("load_test") or {}
    if lt.get("status") in (None, "", "not run", "disabled", "blocked") and not lt.get("endpoints"):
        lrows = f"<tr><td colspan='13'>{_e(lt.get('status', 'not run'))}: {_e(lt.get('reason', 'not requested for this run'))}</td></tr>"
    else:
        lrows = ""
        for ep in lt.get("endpoints", []):
            lrows += (f"<tr class='t-{_e(ep.get('status'))}'><td>{_e(ep.get('method', 'GET'))} {_e(ep.get('url'))}</td><td>{_e(lt.get('users'))}</td><td>{_e(lt.get('max_rps'))}</td><td>{_e(ep.get('duration_s'))} s</td>"
                      f"<td>{_e(ep.get('total'))}</td><td>{_e(ep.get('ok'))}</td><td>{_e(ep.get('failed'))}</td><td>{ep.get('error_rate', 0) * 100:.1f}%</td><td>{_e(ep.get('rps'))}</td>"
                      f"<td>{_e(ep.get('p50_ms'))}</td><td>{_e(ep.get('p95_ms'))}</td><td>{_e(ep.get('p99_ms'))} / max {_e(ep.get('max_ms'))}</td><td>{_e(ep.get('status'))}</td></tr>")
    skipped = "".join(f"<li>{_e(s)}</li>" for s in lt.get("skipped", [])[:20])
    return f"""<h2>Performance &amp; Load Testing</h2>
<h3>Mobile device performance</h3>
<p><small>Measured on the device during the run (cold start, rendering, memory). Not the same as the screen load times in the Screens table.</small></p>
<table><thead><tr><th>Check</th><th>Expected</th><th>Observed</th><th>Status</th></tr></thead><tbody>{drows or '<tr><td colspan="4">No device performance checks ran.</td></tr>'}</tbody></table>
<h3>Backend API load test</h3>
<p><small>Target: {_e(lt.get('base_url', '—'))} · {_e(lt.get('users', '—'))} concurrent users · at most {_e(lt.get('max_rps', '—'))} requests/s · {_e(lt.get('duration_s', '—'))} s per endpoint · GET only.
Thresholds: error rate ≤ {_e(lt.get('max_error_rate', '—'))}, p95 ≤ {_e(lt.get('p95_limit_ms', '—'))} ms.</small></p>
<table><thead><tr><th>Endpoint</th><th>Users</th><th>Max RPS</th><th>Duration</th><th>Requests</th><th>OK</th><th>Failed</th><th>Error rate</th><th>Req/s</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>Status</th></tr></thead><tbody>{lrows}</tbody></table>
{('<details><summary>Endpoints not load tested</summary><ul>' + skipped + '</ul></details>') if skipped else ''}"""


def _journey_section(stats: dict) -> str:
    j = stats.get("journeys") or {}
    rows = "".join(f"<tr class='t-{_e(x.get('status'))}'><td>{_e(x.get('name'))}</td><td>{_e(x.get('source'))}</td><td>{_e(x.get('kind'))}</td><td>{_e(x.get('status'))}</td>"
                   f"<td>{_e(x.get('done'))}/{_e(x.get('steps'))}</td><td>{_e(x.get('reason'))}</td></tr>" for x in j.get("items", []))
    qa = stats.get("qa_data") or {}
    qrows = "".join(f"<tr><td><code>{_e(r.get('id'))}</code></td><td>{_e(r.get('entity'))}</td><td>{_e(r.get('where'))}</td><td>{_e(r.get('journey'))}</td><td>{_e(r.get('status'))}</td><td>{_e(r.get('note'))}</td></tr>" for r in qa.get("records", []))
    return f"""<h2>Journeys and QA test data</h2>
<p><small>Planner: {_e(j.get('planner', 'code'))}. Every step was checked by the safety rules before it ran; only data this run created was changed or deleted.</small></p>
<table><thead><tr><th>Journey</th><th>Planned by</th><th>Kind</th><th>Status</th><th>Steps done</th><th>Reason / result</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No journeys ran.</td></tr>'}</tbody></table>
<table><thead><tr><th>QA data</th><th>What</th><th>Where</th><th>Created by</th><th>Cleanup</th><th>Note</th></tr></thead><tbody>{qrows or '<tr><td colspan="6">This run created no QA data.</td></tr>'}</tbody></table>"""


def _held_section(stats: dict) -> str:
    held = stats.get("held_actions") or []
    rows = "".join(f"<tr><td>{_e(h.get('screen'))}</td><td>{_e(h.get('label'))}</td><td>{_e(h.get('class'))}</td><td>{_e(h.get('reason'))}</td></tr>" for h in held[:300])
    return f"""<h2>Tested vs held back</h2>
<p><small>{_e(stats.get('safe_actions', stats.get('taps', 0)))} actions ran; {_e(len(held))} were held by the safety guard and never tapped.</small></p>
<details><summary>Held actions</summary><table><thead><tr><th>Screen</th><th>Control</th><th>Class</th><th>Why it was held</th></tr></thead><tbody>{rows or '<tr><td colspan="4">None.</td></tr>'}</tbody></table></details>"""


def _test_table(tests: list[TestRecord], run_dir: Path, run_id: str) -> str:
    rows = []
    order = {"fail": 0, "flaky": 1, "needs_review": 2, "blocked": 3, "pass": 4}
    for t in sorted(tests, key=lambda t: (order.get(t.status, 9), t.category, t.route)):
        rows.append(f"<tr class='t-{_e(t.status)}'><td>{_e(t.name)}</td><td>{_e(t.category)}<br><small>{_e(t.technique)}</small></td><td>{_e(t.route)}</td><td>{_steps(t.steps)}</td>"
                    f"<td>{_e(t.expected)}</td><td>{_e(t.observed)}</td><td>{_e(t.status)}</td><td>{_e(t.duration_ms)} ms</td><td>{_evidence(t.evidence, run_dir, run_id)}</td>"
                    f"<td><small>{_e(t.code_ref)}</small></td><td><small>{_e(t.qa_data)}</small></td><td><small>{_e(t.cleanup)}</small></td></tr>")
    return ("<h2>Tests</h2><table><thead><tr><th>Test</th><th>Category</th><th>Screen</th><th>Steps</th><th>Expected</th><th>Observed</th><th>Status</th><th>Duration</th>"
            "<th>Evidence</th><th>Code</th><th>QA data</th><th>Cleanup</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def write_report(run_dir: Path, run_id: str, meta: dict, stats: dict, pages: list[ObservedPage], components: list[ComponentRecord], tests: list[TestRecord], findings: list[FindingRecord]) -> Path:
    native = stats.get("platform") in NATIVE
    seen = set()
    uniq = []
    for f in findings:
        if f.fingerprint not in seen:
            seen.add(f.fingerprint)
            uniq.append(f)
    uniq.sort(key=lambda f: (0 if f.status == "reported" else 1, SEV_ORDER.get(f.severity, 9), f.route))
    rows = []
    for f in uniq:
        fix = (f"<br><small><b>Fix in {_e(f.fix_in)}</b>{(' · ' + _e(f.code_ref)) if f.code_ref else ''}: {_e(f.fix[:400])}</small>" if getattr(f, "fix", "") else "")
        rows.append(
            f"<tr class='sev-{_e(f.severity)} st-{_e(f.status)}'><td>{_e(f.severity)}</td><td>{_e(f.status)}</td><td>{_e(f.category)}</td><td>{_e(f.route)}</td>"
            f"<td><b>{_e(f.title)}</b><br><small>{_e(f.description[:400])}</small>{fix}</td><td>{_e(f.expected)}</td><td>{_e(f.observed)}</td>"
            f"<td>{_e(f.standard_ref)}</td><td>{f.confidence:.2f} ({f.reproduced_n}/{f.reproduced_m})</td><td>{_evidence(f.evidence, run_dir, run_id)}</td></tr>"
        )
    comp_rows = []
    for c in sorted(components, key=lambda c: (c.route, c.kind, c.label or c.name)):
        via = " > ".join(s.get("name") or "?" for s in (c.reveal or []))
        ai = (c.intent or {}).get("llm_expected")
        comp_rows.append(f"<tr class='cst-{_e(c.status)}'><td>{_e(c.route)}</td><td>{_e(c.kind)}</td><td>{_e(c.label or c.name)}</td><td>{_e(c.source)}</td><td><small>{_e(via)}</small></td><td>{_e(c.action_class)}</td><td>{_e(c.status)}</td><td><small>{_e(c.code_file)}{(':' + str(c.code_line)) if c.code_line else ''}</small></td><td><small>{_e('; '.join((c.intent or {}).get('expected', [])[:3]))}{('<br><i>AI: ' + _e(ai) + '</i>') if ai else ''}</small></td><td><small>{_e(c.notes)}</small></td></tr>")
    page_rows = []
    for p in pages:
        issues = sum(len(v.get(x, []) or []) for v in p.viewports.values() if isinstance(v, dict) for x in ("overflowing", "overlaps", "smallTargets", "clipped"))
        shots = " ".join(x for x in (_link(v.get("screenshot", ""), run_dir, run_id, k) for k, v in p.viewports.items() if isinstance(v, dict) and v.get("screenshot")) if x)
        page_rows.append(f"<tr><td>{_e(p.route)}</td><td>{_e(p.title)}</td><td>{_e(p.status_code)}</td><td>{len(p.elements)}</td><td>{len(p.console_errors) + len(p.page_errors)}</td><td>{len(p.failed_requests)}</td><td>{issues}</td><td>{p.load_ms} ms</td><td>{shots}</td></tr>")
    test_counts = {}
    by_tech: dict[str, dict[str, int]] = {}
    for t in tests:
        test_counts[t.status] = test_counts.get(t.status, 0) + 1
        by_tech.setdefault(t.technique, {}).setdefault(t.status, 0)
        by_tech[t.technique][t.status] += 1
    technique_rows = "".join(f"<tr><td>{_e(k)}</td><td>{v.get('pass', 0)}</td><td>{v.get('fail', 0)}</td><td>{v.get('flaky', 0)}</td><td>{v.get('blocked', 0)}</td><td>{v.get('needs_review', 0)}</td></tr>" for k, v in sorted(by_tech.items()))
    if native:
        summary = _native_summary(stats, pages, tests, uniq)
    else:
        summary = f"""<div class="stats">
<div class="stat"><b>{stats.get('code_interactive', 0)}</b>components in code</div>
<div class="stat"><b>{stats.get('matched', 0)}</b>found on live app</div>
<div class="stat"><b>{stats.get('coverage_interaction', stats.get('coverage', 0)) * 100:.0f}%</b>of code components found ({stats.get('coverage', 0) * 100:.0f}% at rest, {stats.get('coverage_unconditional', 0) * 100:.0f}% of always-rendered)</div>
<div class="stat"><b>{stats.get('tested_ratio', 0) * 100:.0f}%</b>of found components tested</div>
<div class="stat"><b>{len(pages)}</b>pages explored</div>
<div class="stat"><b>{len(tests)}</b>tests ({', '.join(f'{k} {v}' for k, v in sorted(test_counts.items()))})</div>
<div class="stat"><b>{sum(1 for f in uniq if f.status == 'reported')}</b>findings reported ({sum(1 for f in uniq if f.status != 'reported')} need review)</div>
</div>"""
    sources = "".join(f"<br><small>{_e(s.get('role'))}: {_e(s.get('source'))}{(' @ ' + _e(s.get('commit_sha', '')[:10])) if s.get('commit_sha') else ''}{(' — ' + _e(s.get('note'))) if s.get('note') else ''}</small>" for s in meta.get("sources", []))
    native_sections = (_journey_section(stats) + _perf_section(stats, tests) + _held_section(stats)) if native else ""
    noun = "Screens" if native else "Pages"
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>QA report {run_id}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#1b2220}}table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 24px}}th,td{{border:1px solid #d3dad6;padding:6px 8px;vertical-align:top;text-align:left}}th{{background:#f0f3f1}}
.sev-critical td:first-child{{background:#f9e4e1;font-weight:700}}.sev-high td:first-child{{background:#fbeed5}}.sev-medium td:first-child{{background:#e8f0ee}}.st-needs_review{{opacity:.75}}
.cst-fail td:nth-child(7){{color:#b3362c;font-weight:700}}.cst-pass td:nth-child(7){{color:#2e7a4d}}.t-fail td:nth-child(7),.t-fail td:last-child{{color:#b3362c;font-weight:700}}.t-blocked{{color:#6b6f6d}}
.stats{{display:flex;gap:16px;flex-wrap:wrap}}.stat{{border:1px solid #d3dad6;border-radius:6px;padding:10px 14px}}.stat b{{display:block;font-size:22px}}pre{{white-space:pre-wrap;max-width:60ch;font-size:11px}}ol{{margin:0;padding-left:18px}}</style></head><body>
<h1>QA report · run {_e(run_id)}</h1>
<p>{_e(meta.get('live_url'))} · {_e(meta.get('repo_url'))} @ {_e(meta.get('commit_sha', '')[:10])} · {_e(meta.get('framework'))} · {_e(meta.get('finished_at'))}{sources}</p>
{summary}
<h2>Findings</h2>
<table><thead><tr><th>Severity</th><th>Status</th><th>Category</th><th>Route</th><th>Finding</th><th>Expected</th><th>Observed</th><th>Standard</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="10">No findings.</td></tr>'}</tbody></table>
{native_sections}
<h2>{noun}</h2>
<table><thead><tr><th>Route</th><th>Title</th><th>HTTP</th><th>Controls</th><th>Console errors</th><th>Failed requests</th><th>Layout issues</th><th>{'Screen' if native else 'Page'} load time</th><th>Screenshots</th></tr></thead><tbody>{''.join(page_rows)}</tbody></table>
<h2>Tests by technique</h2>
<table><thead><tr><th>Technique</th><th>Pass</th><th>Fail</th><th>Flaky</th><th>Blocked</th><th>Needs review</th></tr></thead><tbody>{technique_rows}</tbody></table>
{_test_table(tests, run_dir, run_id) if native else ''}
<h2>Components</h2>
<table><thead><tr><th>Route</th><th>Kind</th><th>Label</th><th>Source</th><th>Reached via</th><th>Action class</th><th>Status</th><th>Code</th><th>Expected (from code)</th><th>Notes</th></tr></thead><tbody>{''.join(comp_rows)}</tbody></table>
</body></html>"""
    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
