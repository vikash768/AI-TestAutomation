"""Persist a finished run: pages, components, tests, findings."""
from __future__ import annotations

from sqlalchemy.orm import Session

from qaplatform.db.models import Component, Finding, Page, Run, Test
from qaplatform.execute.smoke import ComponentRecord, FindingRecord, TestRecord
from qaplatform.explore.crawler import ObservedPage


def persist(db: Session, run: Run, pages: list[ObservedPage], components: list[ComponentRecord], tests: list[TestRecord], findings: list[FindingRecord]) -> dict:
    for p in pages:
        db.add(Page(run_id=run.id, route=p.route, url=p.url, title=p.title, protected=p.protected, status_code=p.status_code, element_count=len(p.elements),
                    console_errors=list(dict.fromkeys(p.page_errors + p.console_errors))[:20], failed_requests=p.failed_requests[:30],
                    viewports={k: {kk: vv for kk, vv in v.items() if kk in ("screenshot", "width", "height", "horizontalOverflow", "scrollW", "docW")} | {"issues": sum(len(v.get(x, []) or []) for x in ("overflowing", "overlaps", "smallTargets", "clipped"))} for k, v in p.viewports.items()},
                    screenshot=p.screenshot, load_ms=p.load_ms))
    db.flush()

    comp_ids: dict[str, int] = {}
    for c in components:
        row = Component(run_id=run.id, component_key=c.key[:400], route=c.route, kind=c.kind, label=(c.label or c.name or "")[:500], source=c.source, code_file=c.code_file, code_line=c.code_line,
                        handler=c.handler or "", selector=c.selector or "", attrs={**c.attrs, "role": c.role, "name": c.name, "href": c.href, "visible": c.visible, "enabled": c.enabled, "page_url": c.page_url,
                                                                                   "reveal": [{"name": s.get("name"), "selector": s.get("selector")} for s in (c.reveal or [])]},
                        intent=c.intent, action_class=c.action_class, conditional=c.conditional, status=c.status, notes=c.notes)
        db.add(row)
        db.flush()
        c.db_id = row.id
        comp_ids[c.key] = row.id

    test_ids: dict[str, int] = {}
    for t in tests:
        row = Test(run_id=run.id, component_id=comp_ids.get(t.component_key or ""), test_key=t.test_key[:400], name=t.name, technique=t.technique, category=t.category, route=t.route,
                   steps=t.steps, expected=t.expected, observed=t.observed, status=t.status, duration_ms=t.duration_ms, reproduced_n=t.reproduced_n, reproduced_m=t.reproduced_m)
        db.add(row)
        db.flush()
        t.db_id = row.id
        test_ids[t.test_key] = row.id

    seen: set[str] = set()
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    needs_review = 0
    for f in findings:
        if f.fingerprint in seen:
            continue
        seen.add(f.fingerprint)
        db.add(Finding(run_id=run.id, test_id=test_ids.get(f.test_key or ""), component_id=comp_ids.get(f.component_key or ""), fingerprint=f.fingerprint, route=f.route, category=f.category,
                       severity=f.severity, title=f.title, description=f.description, expected=f.expected, observed=f.observed, standard_ref=f.standard_ref, viewport=f.viewport,
                       confidence=f.confidence, reproduced_n=f.reproduced_n, reproduced_m=f.reproduced_m, status=f.status, evidence=f.evidence))
        if f.status == "reported":
            counts[f.severity] = counts.get(f.severity, 0) + 1
        else:
            needs_review += 1
    db.flush()
    return {"findings_by_severity": counts, "findings_needs_review": needs_review, "findings_total": len(seen)}
