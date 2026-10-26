"""Prometheus metrics computed from the database at scrape time."""
from __future__ import annotations

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select

from qaplatform.db.models import Component, Finding, ReviewLabel, Run
from qaplatform.db.session import session


class RunCollector:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def collect(self):
        runs_total = GaugeMetricFamily("qa_runs_total", "Runs by status", labels=["status"])
        coverage = GaugeMetricFamily("qa_latest_coverage_ratio", "Share of code-declared interactive components found on the live app, latest completed run per project", labels=["project"])
        coverage_u = GaugeMetricFamily("qa_latest_coverage_unconditional_ratio", "Coverage counting only always-rendered components", labels=["project"])
        coverage_i = GaugeMetricFamily("qa_latest_coverage_interaction_ratio", "Code components found after opening menus, drawers and tabs", labels=["project"])
        tested = GaugeMetricFamily("qa_latest_tested_ratio", "Share of found components that a test exercised", labels=["project"])
        findings = GaugeMetricFamily("qa_latest_findings", "Findings in the latest completed run", labels=["project", "severity", "status"])
        comps = GaugeMetricFamily("qa_latest_components", "Components in the latest completed run by status", labels=["project", "status"])
        duration = GaugeMetricFamily("qa_latest_run_duration_seconds", "Duration of the latest completed run", labels=["project"])
        precision = GaugeMetricFamily("qa_latest_precision_ratio", "Reviewed precision of the latest run (-1 when nothing reviewed)", labels=["project"])
        last_ts = GaugeMetricFamily("qa_latest_run_timestamp_seconds", "Unix time of the latest completed run", labels=["project"])
        info = GaugeMetricFamily("qa_latest_run_info", "Latest run id per project", labels=["project", "run_id", "status"])
        try:
            with session(self.database_url) as db:
                for status, n in db.execute(select(Run.status, func.count()).group_by(Run.status)).all():
                    runs_total.add_metric([status], n)
                projects = [r[0] for r in db.execute(select(Run.project).distinct()).all()]
                for project in projects:
                    run = db.execute(select(Run).where(Run.project == project, Run.status == "completed").order_by(Run.finished_at.desc()).limit(1)).scalar_one_or_none()
                    if run is None:
                        continue
                    stats = run.stats or {}
                    info.add_metric([project, run.id, run.status], 1)
                    coverage.add_metric([project], float(stats.get("coverage", 0.0)))
                    coverage_u.add_metric([project], float(stats.get("coverage_unconditional", 0.0)))
                    coverage_i.add_metric([project], float(stats.get("coverage_interaction", stats.get("coverage", 0.0))))
                    tested.add_metric([project], float(stats.get("tested_ratio", 0.0)))
                    if run.started_at and run.finished_at:
                        duration.add_metric([project], (run.finished_at - run.started_at).total_seconds())
                    if run.finished_at:
                        last_ts.add_metric([project], run.finished_at.timestamp())
                    for sev, st, n in db.execute(select(Finding.severity, Finding.status, func.count()).where(Finding.run_id == run.id).group_by(Finding.severity, Finding.status)).all():
                        findings.add_metric([project, sev, st], n)
                    for st, n in db.execute(select(Component.status, func.count()).where(Component.run_id == run.id).group_by(Component.status)).all():
                        comps.add_metric([project, st], n)
                    labels = db.execute(select(ReviewLabel.verdict, func.count()).join(Finding, Finding.id == ReviewLabel.finding_id).where(Finding.run_id == run.id).group_by(ReviewLabel.verdict)).all()
                    tp = sum(n for v, n in labels if v == "true_positive")
                    fp = sum(n for v, n in labels if v == "false_positive")
                    precision.add_metric([project], (tp / (tp + fp)) if (tp + fp) else -1.0)
        except Exception:
            pass
        yield runs_total
        yield info
        yield coverage
        yield coverage_u
        yield coverage_i
        yield tested
        yield findings
        yield comps
        yield duration
        yield precision
        yield last_ts


def build_registry(database_url: str) -> CollectorRegistry:
    registry = CollectorRegistry()
    registry.register(RunCollector(database_url))
    return registry
