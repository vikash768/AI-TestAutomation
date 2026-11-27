"""Self-contained HTML report written into the run folder.

The report is served at /runs/{run_id}/report while the run's files are served
under /artifacts/, so every evidence link is root-relative
(/artifacts/runs/{run_id}/...), checked to stay inside the run folder, and only
emitted when the file exists.
"""
from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedPage

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
NATIVE = ("android", "ios", "macos")
DEVICE_PERF_TECHNIQUES = ("launch", "performance")
REPORT_CSS = Path(__file__).with_name("report.css")
REPORT_NAVIGATION = Path(__file__).with_name("navigation.js")
REPORT_LAYOUT_MARKER = '<meta name="qa-report-layout" content="2">'
REPORT_NAVIGATION_MARKER = '<meta name="qa-report-navigation" content="1">'
SCREEN_RESULTS_MARKER = '<meta name="qa-screen-results" content="1">'
CHECK_STATES = (
    ("pass", "Passed"), ("fail", "Failed"), ("blocked", "Blocked"),
    ("flaky", "Flaky"), ("needs_review", "Needs review"),
)
STATUS_LABELS = dict(CHECK_STATES)
CHECK_PRIORITY = {state: i for i, state in enumerate(("fail", "flaky", "needs_review", "blocked", "pass"))}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _badge(value: str, kind: str = "status", *, label: str = "") -> str:
    value = value or "unknown"
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    label = label or STATUS_LABELS.get(value, value.replace("_", " ").capitalize())
    return f'<span class="badge {kind}-{slug}">{_e(label)}</span>'


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
    return f'<a class="evidence-link" href="{_e(href)}" target="_blank" rel="noopener">{_e(text)}</a>' if href else ""


def _evidence(items: list, run_dir: Path, run_id: str) -> str:
    ev = []
    for item in items or []:
        if item.get("type") == "screenshot" and item.get("path"):
            viewport = item.get("viewport")
            label = "Screenshot" if viewport in (None, "", "device") else f"Screenshot · {viewport}"
            ev.append(_link(item["path"], run_dir, run_id, label) or "<small>Screenshot missing</small>")
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


def _leg_outcomes(stats: dict) -> str:
    legs = stats.get("legs") or {}
    rows = []
    for scope in ("frontend", "backend"):
        result = legs.get(scope)
        if result:
            rows.append(f'<div class="leg"><strong>{scope.capitalize()}</strong>{_badge(result.get("status"))}<span>{_e(result.get("detail"))}</span></div>')
    if not rows:
        return ""
    return '<section class="outcomes" aria-label="Testing outcomes">' + "".join(rows) + "</section>"


def _native_summary(stats: dict, pages: list, tests: list[TestRecord], uniq: list[FindingRecord]) -> str:
    by = stats.get("tests_by_status") or {}
    qa = stats.get("qa_data") or {}
    lt = stats.get("load_test") or {}
    j = stats.get("journeys") or {}
    backend = stats.get("backend") or {}
    mode = stats.get("test_mode")
    backend_stats = [_stat(backend.get("routes", 0), "backend routes identified")] if mode in ("backend", "full") else []
    outcomes = [
        _stat(len(tests), "checks recorded"),
        _stat(by.get("pass", 0), "pass"),
        _stat(by.get("fail", 0) + by.get("flaky", 0), "fail"),
        _stat(by.get("blocked", 0), "blocked"),
        _stat(by.get("needs_review", 0) + sum(1 for f in uniq if f.status != "reported"), "needs review"),
    ]
    if mode == "backend":
        return '<div class="stats">' + "".join(backend_stats + outcomes + [
            _stat(lt.get("status", "not run"), "load test"),
            _stat(sum(1 for f in uniq if f.status == "reported"), "findings reported"),
        ]) + "</div>"
    if stats.get("code_available") is False:
        coverage = [
            _stat(stats.get("observed_unmatched_visible", 0), "controls observed"),
            _stat("N/A", "Source coverage unavailable"),
        ]
    else:
        coverage = [
            _stat(stats.get("code_interactive", 0), "code components"),
            _stat(stats.get("matched_interaction", stats.get("matched", 0)), "live components found"),
            _stat(f"{stats.get('coverage_interaction', stats.get('coverage', 0)) * 100:.0f}%", "coverage of code components"),
        ]
    journey_stats = [] if mode else [
        _stat(f"{j.get('completed', 0)}/{j.get('total', 0)}", "full journeys completed"),
        _stat(qa.get("created", 0), "QA records created"),
        _stat(f"{qa.get('cleaned', 0)}" + (f" ({qa.get('leftover', 0)} left)" if qa.get("leftover") else ""), "QA records cleaned"),
    ]
    load_stats = [] if mode == "frontend" else [_stat(lt.get("status", "not run"), "load test")]
    return '<div class="stats">' + "".join(coverage + backend_stats + [
        _stat(f"{stats.get('tested_components', 0)} ({stats.get('tested_ratio', 0) * 100:.0f}%)", "components exercised"),
        _stat(stats.get("screens", len(pages)), "screens explored"),
    ] + outcomes + [
        _stat(stats.get("safe_actions", stats.get("taps", 0)), "safe actions executed"),
        _stat(stats.get("held_back", 0), "actions held by safety guard"),
    ] + journey_stats + load_stats + [
        _stat(sum(1 for f in uniq if f.status == "reported"), "findings reported"),
    ]) + "</div>"


def _perf_section(stats: dict, tests: list[TestRecord]) -> str:
    device = [t for t in tests if t.technique in DEVICE_PERF_TECHNIQUES]
    drows = "".join(f"<tr><td>{_e(t.name)}</td><td>{_e(t.expected)}</td><td>{_e(t.observed)}</td><td>{_badge(t.status)}</td></tr>" for t in device)
    lt = stats.get("load_test") or {}
    desktop = stats.get("platform") == "macos"
    title = "macOS app performance" if desktop else "Mobile device performance"
    description = "Desktop app launch checks." if desktop else "Device-level measurements for cold start, rendering, and memory."
    device_summary = "" if stats.get("test_mode") == "backend" else f"""<h3>{title}</h3>
<p>{description}</p>
<div class="table-wrap" tabindex="0" role="region" aria-label="Device performance checks">
<table><thead><tr><th>Check</th><th>Expected</th><th>Observed</th><th>Status</th></tr></thead><tbody>{drows or '<tr><td colspan="4">No device performance checks ran.</td></tr>'}</tbody></table></div>"""
    if stats.get("test_mode") == "frontend":
        return f"<h2>Performance</h2>{device_summary}"
    lrows = "".join(
        f"<tr><td>{_e(ep.get('method', 'GET'))} {_e(ep.get('url'))}</td><td>{_e(lt.get('users'))}</td><td>{_e(lt.get('max_rps'))}</td><td>{_e(ep.get('duration_s'))} s</td>"
        f"<td>{_e(ep.get('total'))}</td><td>{_e(ep.get('ok'))}</td><td>{_e(ep.get('failed'))}</td><td>{ep.get('error_rate', 0) * 100:.1f}%</td><td>{_e(ep.get('rps'))}</td>"
        f"<td>{_e(ep.get('p50_ms'))}</td><td>{_e(ep.get('p95_ms'))}</td><td>{_e(ep.get('p99_ms'))} / max {_e(ep.get('max_ms'))}</td><td>{_badge(ep.get('status'))}</td></tr>"
        for ep in lt.get("endpoints", [])
    )
    load_table = f"""<div class="table-wrap" tabindex="0" role="region" aria-label="Backend API load test results">
<table><thead><tr><th>Endpoint</th><th>Users</th><th>Max RPS</th><th>Duration</th><th>Requests</th><th>OK</th><th>Failed</th><th>Error rate</th><th>Req/s</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>Status</th></tr></thead><tbody>{lrows}</tbody></table></div>""" if lrows else ""
    error_limit = lt.get("max_error_rate")
    error_threshold = f"{error_limit:.1%}" if error_limit is not None else "—"
    load_configuration = f"""<p>{_e(lt.get('base_url', '—'))} · {_e(lt.get('users', '—'))} concurrent users · at most {_e(lt.get('max_rps', '—'))} requests/s · {_e(lt.get('duration_s', '—'))} s per endpoint · GET only.
The thresholds are error rate ≤ {error_threshold} and p95 ≤ {_e(lt.get('p95_limit_ms', '—'))} ms.</p>""" if lt.get("base_url") else ""
    load_status = "" if lrows else f"<p>{_badge(lt.get('status') or 'not run')} {_e(lt.get('reason', 'Not requested for this run'))}</p>"
    skipped = "".join(f"<li>{_e(s)}</li>" for s in lt.get("skipped", [])[:20])
    return f"""<h2>Performance &amp; Load Testing</h2>
{device_summary}
<h3>Backend API load test</h3>
{load_status}{load_configuration}{load_table}
{('<details><summary>Endpoints not load tested</summary><ul>' + skipped + '</ul></details>') if skipped else ''}"""


def _journey_section(stats: dict) -> str:
    j = stats.get("journeys") or {}
    rows = "".join(f"<tr><td>{_e(x.get('name'))}</td><td>{_e(x.get('source'))}</td><td>{_e(x.get('kind'))}</td><td>{_badge(x.get('status'))}</td>"
                   f"<td>{_e(x.get('done'))}/{_e(x.get('steps'))}</td><td>{_e(x.get('reason'))}</td></tr>" for x in j.get("items", []))
    qa = stats.get("qa_data") or {}
    qrows = "".join(f"<tr><td><code>{_e(r.get('id'))}</code></td><td>{_e(r.get('entity'))}</td><td>{_e(r.get('where'))}</td><td>{_e(r.get('journey'))}</td><td>{_e(r.get('status'))}</td><td>{_e(r.get('note'))}</td></tr>" for r in qa.get("records", []))
    return f"""<h2>Journeys and QA test data</h2>
<p>Planner · {_e(j.get('planner', 'code'))}. The runner checks each step against the safety rules and tracks changes in the QA data ledger.</p>
<div class="table-wrap" tabindex="0" role="region" aria-label="Journey results">
<table><thead><tr><th>Journey</th><th>Planned by</th><th>Kind</th><th>Status</th><th>Steps done</th><th>Reason / result</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No journeys ran.</td></tr>'}</tbody></table></div>
<div class="table-wrap" tabindex="0" role="region" aria-label="QA test data">
<table><thead><tr><th>QA data</th><th>What</th><th>Where</th><th>Created by</th><th>Cleanup</th><th>Note</th></tr></thead><tbody>{qrows or '<tr><td colspan="6">This run created no QA data.</td></tr>'}</tbody></table></div>"""


def _held_section(stats: dict) -> str:
    held = stats.get("held_actions") or []
    rows = "".join(f"<tr><td>{_e(h.get('screen'))}</td><td>{_e(h.get('label'))}</td><td>{_e(h.get('class'))}</td><td>{_e(h.get('reason'))}</td></tr>" for h in held[:300])
    return f"""<h2>Tested vs held back</h2>
<p>{_e(stats.get('safe_actions', stats.get('taps', 0)))} actions ran and the safety guard held {_e(len(held))} back.</p>
<details><summary>Held actions</summary><div class="table-wrap" tabindex="0" role="region" aria-label="Held actions">
<table><thead><tr><th>Screen</th><th>Control</th><th>Class</th><th>Why it was held</th></tr></thead><tbody>{rows or '<tr><td colspan="4">None.</td></tr>'}</tbody></table></div></details>"""


def _desktop_coverage_section(stats: dict, run_dir: Path, run_id: str) -> str:
    coverage = stats.get("desktop_coverage")
    if not coverage:
        return ""
    summary = ("The explorer exercised the discovered safe controls." if coverage.get("complete_within_scope")
               else "Some desktop views or controls could not be tested in this run.")
    metrics = "".join(_stat(coverage.get(key, 0), label) for key, label in (
        ("states_walked", "views inspected"), ("controls_observed", "controls discovered"),
        ("exercised", "controls exercised"), ("held", "held back"), ("unreachable", "unreached"),
    ))
    role_labels = {"combobox": "Dropdown", "menuitem": "Menu item", "radio": "Radio button", "switch": "Toggle switch", "treeitem": "Tree item"}
    types = "".join(
        f"<tr><th scope='row'>{_e(role_labels.get(role, role.title()))}</th><td>{_e(counts.get('observed', 0))}</td>"
        f"<td>{_e(counts.get('exercised', 0))}</td><td>{_e(counts.get('held', 0))}</td><td>{_e(counts.get('unreachable', 0))}</td></tr>"
        for role, counts in sorted(coverage.get("control_types", {}).items())
    )
    breakdown = f'''<div class="table-wrap" tabindex="0" role="region" aria-label="Desktop coverage by control type">
<table><thead><tr><th>Control type</th><th>Discovered</th><th>Exercised</th><th>Held</th><th>Unreached</th></tr></thead>
<tbody>{types}</tbody></table></div>''' if types else ""
    notes = dict.fromkeys([*coverage.get("limits", []), *coverage.get("warnings", [])])
    reasons = "<ul>" + "".join(f"<li>{_e(note)}</li>" for note in notes) + "</ul>" if notes else ""
    unreached = "".join(f"<tr><td>{_e(item.get('screen'))}</td><td>{_e(item.get('label') or 'Unnamed control')}</td>"
                        f"<td>{_e(item.get('reason'))}</td></tr>" for item in coverage.get("unreachable_items", []))
    pending = f'''<details><summary>Unreached controls</summary>
<div class="table-wrap" tabindex="0" role="region" aria-label="Unreached desktop controls">
<table><thead><tr><th>Screen</th><th>Control</th><th>Reason</th></tr></thead><tbody>{unreached}</tbody></table></div></details>''' if unreached else ""
    trace = _link("explore.json", run_dir, run_id, "Open exploration details")
    return f'<h2>Desktop coverage</h2><p>{summary}</p><div class="stats">{metrics}</div>{breakdown}{reasons}{pending}{trace}'


def _test_result(*, name: str, meta: str, status: str, duration: str, expected: str, observed: str,
                 steps: str = "", evidence: str = "", facts: tuple[tuple[str, str], ...] = ()) -> str:
    """Render a test with escaped text fields and preformatted steps and evidence."""
    fact_items = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in facts if value)
    sections = "".join(f'<div class="test-section"><h3>{label}</h3>{content}</div>'
                       for label, content in (("Steps", steps), ("Evidence", evidence)) if content)
    if fact_items:
        sections += f'<dl class="test-facts">{fact_items}</dl>'
    return f"""<details class="test-result">
<summary class="test-summary">
<span class="test-heading"><strong>{_e(name)}</strong><span class="test-meta">{_e(meta)}</span></span>
{_badge(status)}<span class="test-duration">{_e(duration)}</span>
</summary>
<div class="test-body"><dl class="test-comparison">
<div class="test-panel"><dt>Expected</dt><dd>{_e(expected) or '—'}</dd></div>
<div class="test-panel"><dt>Observed</dt><dd>{_e(observed) or '—'}</dd></div>
</dl>{sections}</div></details>"""


def _test_list(tests: list[TestRecord], run_dir: Path, run_id: str) -> str:
    rows = []
    for t in sorted(tests, key=lambda t: (CHECK_PRIORITY.get(t.status, 9), t.category, t.route)):
        rows.append(_test_result(
            name=t.name, meta=" · ".join(value for value in (t.category, t.technique, t.route) if value),
            status=t.status, duration=f"{t.duration_ms} ms", expected=t.expected, observed=t.observed,
            steps=_steps(t.steps), evidence=_evidence(t.evidence, run_dir, run_id),
            facts=(("Code", t.code_ref), ("QA data", t.qa_data), ("Cleanup", t.cleanup)),
        ))
    return '<div class="test-list">' + ("".join(rows) or "<p>No tests were recorded.</p>") + "</div>"


def _screen_outcomes(counts: Counter) -> str:
    return "".join(_badge(status, label=f"{counts[status]} {label.lower()}")
                   for status, label in CHECK_STATES if counts[status])


def _screen_screenshot(pages: list[ObservedPage], title: str, run_dir: Path, run_id: str) -> str:
    shots = {}
    for page in pages:
        paths = [page.screenshot, *(v.get("screenshot", "") for v in page.viewports.values() if isinstance(v, dict))]
        for path in paths:
            if path and Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                if href := artifact_href(path, run_dir, run_id):
                    shots[href] = None
    if not shots:
        return '<p class="screen-no-shot">Screenshot unavailable</p>'
    href, *additional = shots
    others = "".join(f'<a class="evidence-link" href="{_e(url)}" target="_blank" rel="noopener">Screenshot {i}</a>'
                     for i, url in enumerate(additional, start=2))
    return f'''<figure class="screen-screenshot">
<a href="{_e(href)}" target="_blank" rel="noopener" aria-label="Open full screenshot of {_e(title)} in a new tab">
<img src="{_e(href)}" alt="Screenshot of {_e(title)}" loading="lazy" decoding="async">
<span>Open full screenshot <span aria-hidden="true">↗</span></span></a>
{f'<figcaption>{others}</figcaption>' if others else ''}</figure>'''


def _screen_metrics(pages: list[ObservedPage]) -> str:
    viewports = (v for p in pages for v in p.viewports.values() if isinstance(v, dict))
    issues = sum(v.get("issues", sum(len(v.get(key) or []) for key in ("overflowing", "overlaps", "smallTargets", "clipped"))) for v in viewports)
    longest_load = max(p.load_ms for p in pages)
    values = [
        ("Longest screen load" if len(pages) > 1 else "Screen load time", f"{longest_load} ms" if longest_load else ""),
        ("Console errors", sum(len(p.console_errors) + len(p.page_errors) for p in pages)),
        ("Failed requests", sum(len(p.failed_requests) for p in pages)),
        ("Layout issues", issues),
    ]
    rows = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in values if value)
    return f'<dl class="screen-metrics">{rows}</dl>' if rows else ""


def _screen_component_table(components: list[ComponentRecord], tests: list[TestRecord], title: str) -> str:
    if not components:
        return '<p class="screen-empty">No components were recorded on this screen.</p>'
    checks: dict[str, Counter] = defaultdict(Counter)
    for test in tests:
        if test.component_key:
            checks[test.component_key][test.status] += 1
    rows = []
    for component in sorted(components, key=lambda c: (
        min((CHECK_PRIORITY.get(status, 9) for status in checks.get(c.key, {c.status: 1})), default=9),
        (c.name or c.label).casefold(), c.kind,
    )):
        name = component.name or component.label or component.selector or "Unnamed control"
        recorded = checks[component.key]
        results = _screen_outcomes(recorded) or _badge(component.status)
        detail = _e(component.notes) if component.status in ("blocked", "skipped") and not recorded else ""
        kind = {"input": "Text field", "select": "Dropdown", "menuitem": "Menu item", "switch": "Toggle"}.get(component.kind, component.kind.capitalize())
        rows.append(f'''<tr><th scope="row">{_e(name)}</th><td>{_e(kind)}</td>
<td><div class="screen-outcomes">{results}</div>{f'<small>{detail}</small>' if detail else ''}</td></tr>''')
    return f'''<div class="table-wrap screen-components" tabindex="0" role="region" aria-label="Components on {_e(title)}">
<table><thead><tr><th scope="col">Component</th><th scope="col">Type</th><th scope="col">Result</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>'''


def _screen_results(pages: list[ObservedPage], components: list[ComponentRecord], tests: list[TestRecord],
                    run_dir: Path, run_id: str) -> tuple[str, list[ComponentRecord], list[TestRecord]]:
    pages_by_route = defaultdict(list)
    route_by_url = {}
    for page in pages:
        pages_by_route[page.route].append(page)
        if page.url:
            route_by_url[page.url] = page.route
    comp_by_route, tests_by_route = defaultdict(list), defaultdict(list)
    route_by_component = {}
    other_components, other_tests = [], []
    for component in components:
        route = route_by_url.get(component.page_url, component.route)
        if component.source != "code" and route in pages_by_route:
            comp_by_route[route].append(component)
            route_by_component[component.key] = route
        else:
            other_components.append(component)
    for test in tests:
        route = route_by_component.get(test.component_key)
        if route is None and test.technique not in DEVICE_PERF_TECHNIQUES and test.category != "platform":
            route = test.route
        if route in pages_by_route:
            tests_by_route[route].append(test)
        else:
            other_tests.append(test)
    if not pages_by_route:
        return '<h2>Screens</h2><p class="empty-state">No screens were captured.</p>', other_components, other_tests

    titles = {route: views[0].title or route.removeprefix("screen:") or "Untitled screen" for route, views in pages_by_route.items()}
    repeated = Counter(titles.values())
    cards, index = [], []
    for number, (route, views) in enumerate(pages_by_route.items(), start=1):
        route_label = route.removeprefix("screen:")
        path = [label for label in views[0].state.get("path", []) if label]
        title = titles[route]
        if repeated[title] > 1:
            title = path[-1] if path else (route_label or title)
        breadcrumb = " → ".join(path)
        subtitle = breadcrumb if breadcrumb and breadcrumb != title else route_label
        screen_tests = tests_by_route[route]
        screen_components = comp_by_route[route]
        anchor = f"screen-{number}"
        label = _e(title)
        status_counts = Counter(test.status for test in screen_tests)
        counts = _screen_outcomes(status_counts)
        components_html = _screen_component_table(screen_components, screen_tests, title)
        tests_html = (f'<details class="screen-checks"><summary>Checks and evidence ({len(screen_tests)})</summary>'
                      f'{_test_list(screen_tests, run_dir, run_id)}</details>') if screen_tests else ''
        count_label = f"{len(screen_tests)} {'check' if len(screen_tests) == 1 else 'checks'}"
        if status_counts["fail"]:
            count_label += f' · {status_counts["fail"]} failed'
        index.append(f'<li><a href="#{anchor}"><strong>{label}</strong><span>{count_label}</span></a></li>')
        cards.append(f'''<article class="screen-card" id="{anchor}" aria-labelledby="{anchor}-title">
<header class="screen-header"><div><h3 id="{anchor}-title">{label}</h3>
{f'<p>{_e(subtitle)}</p>' if subtitle and subtitle != title else ''}</div>
<div class="screen-outcomes">{counts or '<span class="screen-empty">No recorded checks</span>'}</div></header>
<div class="screen-body">
{_screen_screenshot(views, title, run_dir, run_id)}
<div class="screen-results">{components_html}{_screen_metrics(views)}</div></div>
{tests_html}</article>''')
    screen_index = '<nav class="screen-index" aria-label="Jump to a screen"><ul>' + ''.join(index) + '</ul></nav>' if len(index) > 1 else ""
    return '<h2>Screens</h2>' + screen_index + '<div class="screen-list">' + ''.join(cards) + '</div>', other_components, other_tests


def _finding_card(*, severity: str, status: str, title_html: str, category_html: str = "", route_html: str = "",
                  description_html: str = "", expected_html: str = "", observed_html: str = "", fix_html: str = "",
                  standard_html: str = "", confidence_html: str = "", evidence_html: str = "", steps_html: str = "") -> str:
    tone = severity if severity in SEV_ORDER else "info"
    meta = "".join(f'<span>{value}</span>' for value in (category_html, route_html) if value)
    comparison = "".join(f'<div><dt>{label}</dt><dd>{value}</dd></div>' for label, value in (
        ("Observed", observed_html), ("Expected", expected_html),
    ) if value)
    facts = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in (
        ("Standard", standard_html), ("Confidence", confidence_html),
    ) if value)
    details = "".join((f'<div class="finding-fix">{fix_html}</div>' if fix_html else "",
                       f'<div class="finding-steps"><strong>Steps to reproduce</strong>{steps_html}</div>' if steps_html else "",
                       f'<dl class="finding-facts">{facts}</dl>' if facts else ""))
    extras = f'<details class="finding-details"><summary>Technical details</summary><div>{details}</div></details>' if details else ""
    return f'''<article class="finding-card accent-{tone}" data-severity="{tone}">
<div class="finding-lead">
<div class="finding-badges">{_badge(severity, "severity")}{_badge(status)}</div>
<h3>{title_html}</h3>
{f'<div class="finding-meta">{meta}</div>' if meta else ''}
{f'<p class="finding-description">{description_html}</p>' if description_html else ''}
</div>
{f'<dl class="finding-comparison">{comparison}</dl>' if comparison else ''}
{f'<div class="finding-evidence">{evidence_html}</div>' if evidence_html else ''}
{extras}</article>'''


def _check_overview(counts: dict | None) -> str:
    if counts is None:
        return ""
    outcomes = [(key, label, counts.get(key, 0)) for key, label in CHECK_STATES]
    total = sum(count for _, _, count in outcomes)
    segments = "".join(f'<span class="result-{key}" style="flex-grow:{count}"></span>' for key, _, count in outcomes if count)
    legend = "".join(f'<li><i class="result-{key}" aria-hidden="true"></i><b>{count}</b> {label.lower()}</li>'
                     for key, label, count in outcomes if count or key in ("pass", "fail", "blocked"))
    return f'''<div class="check-overview">
<p><strong>{total}</strong> checks recorded</p>
<div class="result-bar" aria-hidden="true">{segments}</div>
<ul class="result-legend">{legend}</ul></div>'''


def navigation_script() -> str:
    return f'<script>{REPORT_NAVIGATION.read_text(encoding="utf-8")}</script>'


def _report_document(run_id: str, *, mode: str = "", platform: str = "", target: str = "", meta_html: str = "", summary_html: str = "",
                     counts: dict | None = None, legs_html: str = "", screen_html: str = "", findings_html: str = "", sections=()) -> str:
    section_items = [(anchor, label, body) for anchor, label, body in sections if body]
    with_overview = " has-overview" if counts is not None else ""
    scopes = "<span aria-hidden=\"true\"> / </span>".join(_e(x) for x in (platform, mode) if x)
    primary = "screens" if screen_html else "findings"
    links = [("screens", "Screens")] if screen_html else []
    links.append(("findings", "Findings"))
    links.extend((anchor, label) for anchor, label, _ in section_items)
    active_link = ' aria-current="location"'
    nav_html = "".join(f'<a href="#{_e(anchor)}"{active_link if anchor == primary else ""}>{_e(label)}</a>'
                       for anchor, label in links)
    blocks = []
    for anchor, _, body in section_items:
        attribute = f'id="{_e(anchor)}"'
        block_id = "" if attribute in body else " " + attribute
        blocks.append(f'<section class="report-section"{block_id}>{body}</section>')
    section_html = "".join(blocks)
    empty_findings = '<p class="empty-state">No findings were recorded for this run.</p>'
    screen_section = f'<section class="report-section screen-section" id="screens">{screen_html}</section>' if screen_html else ''
    marker = SCREEN_RESULTS_MARKER if screen_html else ''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{REPORT_LAYOUT_MARKER}{REPORT_NAVIGATION_MARKER}{marker}<title>QA report · {_e(run_id)}</title>
<style>{REPORT_CSS.read_text(encoding="utf-8")}</style></head>
<body><a class="skip-link" href="#{primary}">Skip to {primary}</a>
<header class="report-top"><div class="report-width">
  <a class="back-link" href="/runs/{quote(run_id, safe='')}">← Back to run</a>
  <span class="report-id">Run <code>{_e(run_id)}</code></span>
</div></header>
<main class="report-width">
  <section class="report-hero{with_overview}" aria-labelledby="report-title">
    <div class="hero-intro">{f'<div class="report-scope">{scopes}</div>' if scopes else ''}
      <h1 id="report-title">QA report</h1>
      {f'<p class="report-target">{_e(target)}</p>' if target else ''}
      {f'<div class="hero-meta">{meta_html}</div>' if meta_html else ''}
    </div>
    {_check_overview(counts)}
  </section>
  <nav class="jump-nav" aria-label="Report sections">{nav_html}</nav>
  {legs_html}
  {f'<details class="run-metrics"><summary>Run metrics</summary>{summary_html}</details>' if summary_html else ''}
  {screen_section}
  <section class="findings-section" id="findings" aria-labelledby="findings-title">
    <div class="section-heading">
      <h2 id="findings-title">Findings</h2>
      <div class="finding-filters" hidden>
        <label class="visually-hidden" for="finding-search">Search findings</label>
        <input type="search" id="finding-search" placeholder="Search findings">
        <label class="visually-hidden" for="finding-severity">Filter by severity</label>
        <select id="finding-severity"><option value="">All severities</option>{''.join(f'<option value="{s}">{s.capitalize()}</option>' for s in SEV_ORDER)}</select>
      </div>
    </div>
    <div class="finding-list">{findings_html or empty_findings}</div>
    <p class="empty-state" id="finding-empty" hidden>No findings match these filters.</p>
    <p class="visually-hidden" id="finding-count" role="status"></p>
  </section>
  {section_html}
</main>
<script>
(() => {{
  const cards = [...document.querySelectorAll('.finding-card')];
  if (!cards.length) return;
  const search = document.getElementById('finding-search');
  const severity = document.getElementById('finding-severity');
  const empty = document.getElementById('finding-empty');
  const count = document.getElementById('finding-count');
  const update = () => {{
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    for (const card of cards) {{
      card.hidden = Boolean(severity.value && card.dataset.severity !== severity.value) || !card.textContent.toLowerCase().includes(query);
      if (!card.hidden) visible++;
    }}
    empty.hidden = visible > 0;
    count.textContent = `${{visible}} of ${{cards.length}} findings shown`;
  }};
  search.addEventListener('input', update);
  severity.addEventListener('change', update);
  document.querySelector('.finding-filters').hidden = false;
  update();
}})();
</script>
{navigation_script()}
</body></html>'''


def _format_finished(value: str | None) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.strftime("%d %b %Y, %H:%M UTC")


def render_report(run_dir: Path, run_id: str, meta: dict, stats: dict, pages: list[ObservedPage], components: list[ComponentRecord],
                  tests: list[TestRecord], findings: list[FindingRecord], *, archived: bool = False) -> str:
    native = stats.get("platform") in NATIVE
    mode = stats.get("test_mode")
    show_device_evidence = mode != "backend" and (mode != "full" or bool(pages))
    screen_html, other_components, other_tests = "", components, tests
    if native and show_device_evidence:
        screen_html, other_components, other_tests = _screen_results(pages, components, tests, run_dir, run_id)
    seen = set()
    uniq = []
    for f in findings:
        if f.fingerprint not in seen:
            seen.add(f.fingerprint)
            uniq.append(f)
    uniq.sort(key=lambda f: (0 if f.status == "reported" else 1, SEV_ORDER.get(f.severity, 9), f.route))
    cards = []
    for f in uniq:
        fix = ""
        if f.fix or f.code_ref:
            location = f"Fix in {f.fix_in}" if f.fix_in else "Suggested fix"
            code = f"<code>{_e(f.code_ref)}</code>" if f.code_ref else ""
            fix = f'<strong>{_e(location)}</strong> {code}<p>{_e(f.fix)}</p>'
        cards.append(_finding_card(
            severity=f.severity, status=f.status, title_html=_e(f.title), description_html=_e(f.description),
            category_html=_e(f.category), route_html=_e(f.route.removeprefix("screen:")), expected_html=_e(f.expected),
            observed_html=_e(f.observed), fix_html=fix, standard_html=_e(f.standard_ref),
            confidence_html=f"{f.confidence:.2f} ({f.reproduced_n}/{f.reproduced_m} reproduced)",
            steps_html=_steps(f.steps), evidence_html=_evidence(f.evidence, run_dir, run_id),
        ))
    comp_rows = []
    for c in sorted(other_components, key=lambda c: (c.route, c.kind, c.label or c.name)):
        via = " > ".join(s.get("name") or "?" for s in (c.reveal or []))
        ai = (c.intent or {}).get("llm_expected")
        comp_rows.append(f"<tr class='cst-{_e(c.status)}'><td>{_e(c.route)}</td><td>{_e(c.kind)}</td><td>{_e(c.label or c.name)}</td><td>{_e(c.source)}</td><td><small>{_e(via)}</small></td><td>{_e(c.action_class)}</td><td>{_e(c.status)}</td><td><small>{_e(c.code_file)}{(':' + str(c.code_line)) if c.code_line else ''}</small></td><td><small>{_e('; '.join((c.intent or {}).get('expected', [])[:3]))}{('<br><i>AI: ' + _e(ai) + '</i>') if ai else ''}</small></td><td><small>{_e(c.notes)}</small></td></tr>")
    page_rows = []
    for p in pages if not native else []:
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
    backend_section = ""
    if mode in ("backend", "full"):
        inventory = _link("backend.json", run_dir, run_id, "Open backend route inventory")
        backend_section = f"<h2>Backend source review</h2><p>API routes identified from source. {inventory}</p>"
    expected_heading = "Expected" if native and stats.get("code_available") is False else "Expected (from code)"
    device_pages = "" if native else f"""<h2>Pages</h2>
<div class="table-wrap" tabindex="0" role="region" aria-label="Pages inspected"><table><thead><tr><th>Route</th><th>Title</th><th>HTTP</th><th>Controls</th><th>Console errors</th><th>Failed requests</th><th>Layout issues</th><th>Page load time</th><th>Screenshots</th></tr></thead><tbody>{''.join(page_rows)}</tbody></table></div>"""
    component_heading = "Other components" if screen_html else "Components"
    component_section = "" if not show_device_evidence or (screen_html and not comp_rows) else f"""<h2>{component_heading}</h2>
<div class="table-wrap" tabindex="0" role="region" aria-label="Components"><table><thead><tr><th>Route</th><th>Kind</th><th>Label</th><th>Source</th><th>Reached via</th><th>Action class</th><th>Status</th><th>Code</th><th>{expected_heading}</th><th>Notes</th></tr></thead><tbody>{''.join(comp_rows)}</tbody></table></div>"""
    technique_table = f"""<div class="table-wrap" tabindex="0" role="region" aria-label="Tests by technique">
<table><thead><tr><th>Technique</th><th>Pass</th><th>Fail</th><th>Flaky</th><th>Blocked</th><th>Needs review</th></tr></thead><tbody>{technique_rows}</tbody></table></div>"""
    tests_heading = "Tests"
    if native:
        if screen_html:
            tests_heading = "Other checks" if other_tests else "Test breakdown"
        if other_tests or not screen_html:
            tests_section = (f'<h2 id="tests">{tests_heading}</h2>{_test_list(other_tests, run_dir, run_id)}'
                             f'<details class="technique-breakdown"><summary>Tests by technique</summary>{technique_table}</details>')
        else:
            tests_section = f'<h2 id="tests">Tests by technique</h2>{technique_table}'
    else:
        tests_section = f'<h2>Tests by technique</h2>{technique_table}'
    sections = [
        ("desktop-coverage", "Desktop coverage", _desktop_coverage_section(stats, run_dir, run_id)),
        ("tests", tests_heading, tests_section),
        ("pages", "Pages", device_pages),
        ("performance", "Performance", _perf_section(stats, tests) if native else ""),
        ("backend", "Backend", backend_section),
        ("journeys", "Journeys", _journey_section(stats) if native and not mode else ""),
        ("held-actions", "Held actions", _held_section(stats) if native and show_device_evidence else ""),
        ("components", component_heading, component_section),
    ]
    metadata = " · ".join(value for value in (_e(meta.get("framework")), _e(_format_finished(meta.get("finished_at")))) if value)
    sources = []
    for source in meta.get("sources", []):
        role, url = _e((source.get("role") or "").capitalize()), _e(source.get("source"))
        commit = " @ " + _e((source.get("commit_sha") or "")[:10]) if source.get("commit_sha") else ""
        note = " · " + _e(source.get("note")) if source.get("note") else ""
        sources.append(f"<li>{role} · {url}{commit}{note}</li>")
    if sources:
        metadata += f'<details class="run-sources"><summary>Source details</summary><ul>{"".join(sources)}</ul></details>'
    elif meta.get("repo_url"):
        commit = " @ " + _e(meta["commit_sha"][:10]) if meta.get("commit_sha") else ""
        metadata += f'<p>{_e(meta["repo_url"])}{commit}</p>'
    if archived:
        metadata += f'<p>{_link("report.html", run_dir, run_id, "Original report and evidence")}</p>'
    platform = stats.get("platform") or "web"
    label = {"ios": "iOS", "macos": "macOS"}.get(platform, platform.capitalize())
    return _report_document(
        run_id, mode=f"{mode.capitalize()} testing" if mode else "", platform=label,
        target=str(meta.get("live_url") or "").removeprefix(f"{platform}: "), meta_html=metadata, summary_html=summary, counts=test_counts,
        legs_html=_leg_outcomes(stats) if native else "", screen_html=screen_html, findings_html="".join(cards), sections=sections,
    )


def write_report(run_dir: Path, run_id: str, meta: dict, stats: dict, pages: list[ObservedPage], components: list[ComponentRecord], tests: list[TestRecord], findings: list[FindingRecord]) -> Path:
    doc = render_report(run_dir, run_id, meta, stats, pages, components, tests, findings)
    out = run_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
