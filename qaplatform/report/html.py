"""Self-contained HTML report written into the run folder."""
from __future__ import annotations

import html
import json
from pathlib import Path

from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedPage

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _rel(path: str, run_dir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(run_dir.resolve()))
    except Exception:
        return path


def write_report(run_dir: Path, run_id: str, meta: dict, stats: dict, pages: list[ObservedPage], components: list[ComponentRecord], tests: list[TestRecord], findings: list[FindingRecord]) -> Path:
    seen = set()
    uniq = []
    for f in findings:
        if f.fingerprint not in seen:
            seen.add(f.fingerprint)
            uniq.append(f)
    uniq.sort(key=lambda f: (0 if f.status == "reported" else 1, SEV_ORDER.get(f.severity, 9), f.route))
    rows = []
    for f in uniq:
        ev = []
        for item in f.evidence:
            if item.get("type") == "screenshot" and item.get("path"):
                ev.append(f'<a href="{_e(_rel(item["path"], run_dir))}">screenshot{(" " + item["viewport"]) if item.get("viewport") else ""}</a>')
            elif item.get("type") == "console":
                ev.append("<details><summary>console</summary><pre>" + _e("\n".join(item.get("messages", []))) + "</pre></details>")
            elif item.get("type") == "requests":
                ev.append("<details><summary>requests</summary><pre>" + _e("\n".join(f"{r.get('method')} {r.get('url')} -> {r.get('status') or r.get('error')}" for r in item.get("items", []))) + "</pre></details>")
            elif item.get("type") == "layout":
                ev.append("<details><summary>layout</summary><pre>" + _e(json.dumps({k: v for k, v in item.items() if k != "type"}, indent=1)[:2000]) + "</pre></details>")
        rows.append(
            f"<tr class='sev-{_e(f.severity)} st-{_e(f.status)}'><td>{_e(f.severity)}</td><td>{_e(f.status)}</td><td>{_e(f.category)}</td><td>{_e(f.route)}</td>"
            f"<td><b>{_e(f.title)}</b><br><small>{_e(f.description[:400])}</small></td><td>{_e(f.expected)}</td><td>{_e(f.observed)}</td>"
            f"<td>{_e(f.standard_ref)}</td><td>{f.confidence:.2f} ({f.reproduced_n}/{f.reproduced_m})</td><td>{' '.join(ev)}</td></tr>"
        )
    comp_rows = []
    for c in sorted(components, key=lambda c: (c.route, c.kind, c.label or c.name)):
        via = " > ".join(s.get("name") or "?" for s in (c.reveal or []))
        comp_rows.append(f"<tr class='cst-{_e(c.status)}'><td>{_e(c.route)}</td><td>{_e(c.kind)}</td><td>{_e(c.label or c.name)}</td><td>{_e(c.source)}</td><td><small>{_e(via)}</small></td><td>{_e(c.action_class)}</td><td>{_e(c.status)}</td><td><small>{_e(c.code_file)}{(':' + str(c.code_line)) if c.code_line else ''}</small></td><td><small>{_e('; '.join((c.intent or {}).get('expected', [])[:3]))}</small></td><td><small>{_e(c.notes)}</small></td></tr>")
    page_rows = []
    for p in pages:
        issues = sum(len(v.get(x, []) or []) for v in p.viewports.values() if isinstance(v, dict) for x in ("overflowing", "overlaps", "smallTargets", "clipped"))
        shots = " ".join(f'<a href="{_e(_rel(v.get("screenshot", ""), run_dir))}">{_e(k)}</a>' for k, v in p.viewports.items() if isinstance(v, dict) and v.get("screenshot"))
        page_rows.append(f"<tr><td>{_e(p.route)}</td><td>{_e(p.title)}</td><td>{_e(p.status_code)}</td><td>{len(p.elements)}</td><td>{len(p.console_errors) + len(p.page_errors)}</td><td>{len(p.failed_requests)}</td><td>{issues}</td><td>{p.load_ms} ms</td><td>{shots}</td></tr>")
    test_counts = {}
    by_tech: dict[str, dict[str, int]] = {}
    for t in tests:
        test_counts[t.status] = test_counts.get(t.status, 0) + 1
        by_tech.setdefault(t.technique, {}).setdefault(t.status, 0)
        by_tech[t.technique][t.status] += 1
    technique_rows = "".join(f"<tr><td>{_e(k)}</td><td>{v.get('pass', 0)}</td><td>{v.get('fail', 0)}</td><td>{v.get('flaky', 0)}</td><td>{v.get('blocked', 0)}</td></tr>" for k, v in sorted(by_tech.items()))
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>QA report {run_id}</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#1b2220}}table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 24px}}th,td{{border:1px solid #d3dad6;padding:6px 8px;vertical-align:top;text-align:left}}th{{background:#f0f3f1}}
.sev-critical td:first-child{{background:#f9e4e1;font-weight:700}}.sev-high td:first-child{{background:#fbeed5}}.sev-medium td:first-child{{background:#e8f0ee}}.st-needs_review{{opacity:.75}}
.cst-fail td:nth-child(6){{color:#b3362c;font-weight:700}}.cst-pass td:nth-child(6){{color:#2e7a4d}}.stats{{display:flex;gap:16px;flex-wrap:wrap}}.stat{{border:1px solid #d3dad6;border-radius:6px;padding:10px 14px}}.stat b{{display:block;font-size:22px}}pre{{white-space:pre-wrap;max-width:60ch;font-size:11px}}</style></head><body>
<h1>QA report · run {_e(run_id)}</h1>
<p>{_e(meta.get('live_url'))} · {_e(meta.get('repo_url'))} @ {_e(meta.get('commit_sha', '')[:10])} · {_e(meta.get('framework'))} · {_e(meta.get('finished_at'))}</p>
<div class="stats">
<div class="stat"><b>{stats.get('code_interactive', 0)}</b>components in code</div>
<div class="stat"><b>{stats.get('matched', 0)}</b>found on live app</div>
<div class="stat"><b>{stats.get('coverage_interaction', stats.get('coverage', 0)) * 100:.0f}%</b>of code components found ({stats.get('coverage', 0) * 100:.0f}% at rest, {stats.get('coverage_unconditional', 0) * 100:.0f}% of always-rendered)</div>
<div class="stat"><b>{stats.get('tested_ratio', 0) * 100:.0f}%</b>of found components tested</div>
<div class="stat"><b>{len(pages)}</b>pages explored</div>
<div class="stat"><b>{len(tests)}</b>tests ({', '.join(f'{k} {v}' for k, v in sorted(test_counts.items()))})</div>
<div class="stat"><b>{sum(1 for f in uniq if f.status == 'reported')}</b>findings reported ({sum(1 for f in uniq if f.status != 'reported')} need review)</div>
</div>
<h2>Findings</h2>
<table><thead><tr><th>Severity</th><th>Status</th><th>Category</th><th>Route</th><th>Finding</th><th>Expected</th><th>Observed</th><th>Standard</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="10">No findings.</td></tr>'}</tbody></table>
<h2>Pages</h2>
<table><thead><tr><th>Route</th><th>Title</th><th>HTTP</th><th>Controls</th><th>Console errors</th><th>Failed requests</th><th>Layout issues</th><th>Load</th><th>Screenshots</th></tr></thead><tbody>{''.join(page_rows)}</tbody></table>
<h2>Tests by technique</h2>
<table><thead><tr><th>Technique</th><th>Pass</th><th>Fail</th><th>Flaky</th><th>Blocked</th></tr></thead><tbody>{technique_rows}</tbody></table>
<h2>Components</h2>
<table><thead><tr><th>Route</th><th>Kind</th><th>Label</th><th>Source</th><th>Reached via</th><th>Action class</th><th>Status</th><th>Code</th><th>Expected (from code)</th><th>Notes</th></tr></thead><tbody>{''.join(comp_rows)}</tbody></table>
</body></html>"""
    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
