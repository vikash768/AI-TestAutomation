"""Present saved reports from the original HTML generator with the current layout."""
from __future__ import annotations

import re
from html import escape, unescape

from qaplatform.report.html import (
    REPORT_LAYOUT_MARKER, REPORT_NAVIGATION_MARKER, _finding_card, _format_finished, _leg_outcomes, _report_document, _test_result,
    navigation_script,
)


LEGACY_REPORT_PREFIX = '<!doctype html><html><head><meta charset="utf-8"><title>QA report '
_LAYOUT_V2_PREFIX = '<!doctype html>\n<html lang="en"><head>'
_FIXED_FIRST_LINK = '.jump-nav a:first-child, .jump-nav a:hover { color: var(--blue); background: #e6effd; }'
_DOCUMENT = re.compile(
    re.escape(LEGACY_REPORT_PREFIX) + r'[^<>]+</title>\s*'
    r'<style>body\{font-family:system-ui,sans-serif;margin:24px;.*?</style></head><body>\s*'
    r'<h1>QA report · run ([^<>]+)</h1>\s*<p>(.*?)</p>\s*'
    r'(<div class="stats">.*?)\s*(<h2(?: [^<>]*)?>.*?)\s*</body></html>\s*\Z',
    re.DOTALL,
)
_HEADING = re.compile(r'<h2(?: id="([a-z0-9-]+)")?>([^<>]+)</h2>')
_ROWS = re.compile(r'<tr(?: class=[\'"][^<>]*[\'"])?>(.*?)</tr>', re.DOTALL)
_CELLS = re.compile(r'<td(?: [^<>]*)?>(.*?)</td>', re.DOTALL)
_FINDING_BODY = re.compile(r'<b>([^<>]*)</b><br><small>([^<>]*)</small>(.*)', re.DOTALL)
_STATUSES = ("pass", "fail", "flaky", "blocked", "needs_review")
_SECTIONS = {
    "Backend source review": ("backend", "Backend"),
    "Components": ("components", "Components"),
    "Journeys and QA test data": ("journeys", "Journeys"),
    "Pages": ("pages", "Pages"),
    "Performance": ("performance", "Performance"),
    "Performance & Load Testing": ("performance", "Performance"),
    "Screens": ("screens", "Screens"),
    "Tested vs held back": ("held-actions", "Held actions"),
    "Tests": ("tests", "Tests"),
    "Tests by technique": ("techniques", "Test breakdown"),
}
_SECTION_ORDER = ("tests", "screens", "pages", "performance", "backend", "journeys", "held-actions", "components")


def _table_rows(fragment: str) -> list[list[str]]:
    body = fragment.partition("<tbody>")[2].partition("</tbody>")[0]
    return [_CELLS.findall(row) for row in _ROWS.findall(body)]


def _finding_cards(fragment: str) -> str | None:
    rows = _table_rows(fragment)
    if rows == [["No findings."]]:
        return ""
    if not rows or any(len(row) != 10 for row in rows):
        return None
    cards = []
    for severity, status, category, route, body, expected, observed, standard, confidence, evidence in rows:
        if (match := _FINDING_BODY.fullmatch(body)) is None:
            return None
        title, description, fix = match.groups()
        if fix.startswith("<br><small>") and fix.endswith("</small>"):
            fix = fix[len("<br><small>"):-len("</small>")]
        cards.append(_finding_card(
            severity=unescape(severity), status=unescape(status), category_html=category, route_html=route.removeprefix("screen:"),
            title_html=title, description_html=description, expected_html=expected, observed_html=observed,
            fix_html=fix, standard_html=standard, confidence_html=confidence, evidence_html=evidence,
        ))
    return "".join(cards)


def _test_counts(fragment: str) -> dict[str, int] | None:
    rows = _table_rows(fragment)
    if any(len(row) != 6 or not all(count.isdecimal() for count in row[1:]) for row in rows):
        return None
    return dict(zip(_STATUSES, (sum(int(row[i]) for row in rows) for i in range(1, 6))))


def _test_details(fragment: str) -> str | None:
    rows = _table_rows(fragment)
    if not rows or any(len(row) != 12 for row in rows):
        return None
    tests = []
    for name, category, route, steps, expected, observed, status, duration, evidence, code, qa_data, cleanup in rows:
        category_and_technique = category.replace("<br>", " · ").replace("<small>", "").replace("</small>", "").strip(" ·")
        meta = " · ".join(unescape(value) for value in (category_and_technique, route) if value)
        facts = tuple((label, unescape(value.removeprefix("<small>").removesuffix("</small>")))
                      for label, value in (("Code", code), ("QA data", qa_data), ("Cleanup", cleanup)))
        tests.append(_test_result(
            name=unescape(name), meta=meta, status=unescape(status), duration=unescape(duration),
            expected=unescape(expected), observed=unescape(observed), steps=steps, evidence=evidence, facts=facts,
        ))
    return '<div class="test-list">' + "".join(tests) + "</div>"


def _wrap_tables(fragment: str, heading: str) -> str:
    return fragment.replace("<table>", f'<div class="table-wrap" tabindex="0" role="region" aria-label="{escape(heading)}"><table>').replace("</table>", "</table></div>")


def _legacy_outcomes(fragment: str) -> str:
    rows = _table_rows(fragment)
    if all(len(row) == 3 and row[0] in ("Frontend", "Backend") for row in rows):
        return _leg_outcomes({"legs": {
            scope.lower(): {"status": unescape(status.removeprefix("<b>").removesuffix("</b>")), "detail": unescape(detail)}
            for scope, status, detail in rows
        }})
    return ""


def refresh_legacy_report(original: str, run_id: str) -> str | None:
    """Reformat the old generator's saved fragments. Other HTML keeps its original response."""
    match = _DOCUMENT.fullmatch(original)
    if match is None:
        return None
    heading, metadata, metrics, content = match.groups()
    if unescape(heading.split(" · ", 1)[0]) != run_id:
        return None

    target_line, source_break, sources = metadata.partition("<br>")
    target, _, meta = target_line.partition(" · ")
    target = unescape(target)
    mode_match = re.fullmatch(r"[^·]+ · ((?:Frontend|Backend|Full) testing)", heading)
    mode = mode_match[1] if mode_match else ""
    prefix, _, package = target.partition(": ")
    platforms = {"android": "Android", "ios": "iOS", "macos": "macOS"}
    platform = platforms.get(prefix, "Android" if mode else "Web")
    if prefix in platforms and package:
        target = package
    meta = " · ".join(escape(_format_finished(unescape(value))) for value in meta.split(" · "))

    sections, legs, cards, counts = [], "", None, None
    headings = list(_HEADING.finditer(content))
    for i, section in enumerate(headings):
        identifier, raw_heading = section.groups()
        display_heading = unescape(raw_heading)
        fragment = content[section.end():headings[i + 1].start() if i + 1 < len(headings) else len(content)]
        fragment = fragment.replace('<a href="/artifacts/runs/', '<a class="evidence-link" href="/artifacts/runs/').replace(
            'target="_blank">', 'target="_blank" rel="noopener">',
        )
        if display_heading == "Findings":
            cards = _finding_cards(fragment)
            continue
        restyled = _test_details(fragment) if display_heading == "Tests" else None
        fragment_html = f"<h2>{raw_heading}</h2>{restyled or _wrap_tables(fragment, display_heading)}"
        if display_heading == "Testing outcomes":
            legs = _legacy_outcomes(fragment) or fragment_html
            continue
        if display_heading == "Tests by technique":
            counts = _test_counts(fragment)
        key, label = _SECTIONS.get(display_heading, (re.sub(r"[^a-z0-9]+", "-", display_heading.lower()).strip("-"), display_heading))
        sections.append((identifier or key, label, fragment_html))
    if cards is None:
        return None
    techniques = next((body for key, _, body in sections if key == "techniques"), "")
    if techniques:
        if any(key == "tests" for key, _, _ in sections):
            breakdown = '<details class="technique-breakdown"><summary>Tests by technique</summary>' + techniques.partition("</h2>")[2] + "</details>"
            sections = [(key, label, body + (breakdown if key == "tests" else ""))
                        for key, label, body in sections if key != "techniques"]
        else:
            sections = [("tests", "Tests", body) if key == "techniques" else (key, label, body) for key, label, body in sections]
    priorities = {key: index for index, key in enumerate(_SECTION_ORDER)}
    sections.sort(key=lambda section: priorities.get(section[0], len(priorities)))
    return _report_document(
        run_id, mode=mode, platform=platform, target=target, meta_html=meta + (source_break if meta else "") + sources,
        summary_html=metrics, counts=counts, legs_html=legs, findings_html=cards, sections=sections,
    )


def saved_report_needs_refresh(head: str) -> bool:
    return head.startswith(LEGACY_REPORT_PREFIX) or (
        head.startswith(_LAYOUT_V2_PREFIX) and REPORT_LAYOUT_MARKER in head and REPORT_NAVIGATION_MARKER not in head
    )


def refresh_saved_report(original: str, run_id: str) -> str | None:
    if original.startswith(LEGACY_REPORT_PREFIX):
        return refresh_legacy_report(original, run_id)
    if _FIXED_FIRST_LINK not in original or not original.rstrip().endswith('</body></html>'):
        return None
    active_rule = _FIXED_FIRST_LINK.replace('a:first-child, .jump-nav a:hover', 'a[aria-current="location"]')
    return (original.replace(REPORT_LAYOUT_MARKER, REPORT_LAYOUT_MARKER + REPORT_NAVIGATION_MARKER, 1)
            .replace(_FIXED_FIRST_LINK, active_rule, 1)
            .replace('</body></html>', navigation_script() + '\n</body></html>', 1))
