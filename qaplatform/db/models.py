"""Database model: runs, components, pages, tests, findings, evidence.

Grafana reads these tables directly through its Postgres datasource, so the
column names are part of the dashboard contract. Keep them stable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project: Mapped[str] = mapped_column(String(80), default="default")
    repo_url: Mapped[str] = mapped_column(Text)
    live_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    framework: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["RunEvent"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")
    stage: Mapped[str] = mapped_column(String(40), default="")
    message: Mapped[str] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="events")


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    route: Mapped[str] = mapped_column(String(300), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    protected: Mapped[bool | None] = mapped_column(nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    element_count: Mapped[int] = mapped_column(Integer, default=0)
    console_errors: Mapped[list] = mapped_column(JSON, default=list)
    failed_requests: Mapped[list] = mapped_column(JSON, default=list)
    viewports: Mapped[dict] = mapped_column(JSON, default=dict)
    screenshot: Mapped[str] = mapped_column(Text, default="")
    load_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Component(Base):
    __tablename__ = "components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    component_key: Mapped[str] = mapped_column(String(400), index=True)
    route: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    label: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(10), default="code", index=True)  # code | observed | both
    code_file: Mapped[str] = mapped_column(Text, default="")
    code_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handler: Mapped[str] = mapped_column(Text, default="")
    selector: Mapped[str] = mapped_column(Text, default="")
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
    action_class: Mapped[str] = mapped_column(String(20), default="safe", index=True)
    conditional: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="untested", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Test(Base):
    __tablename__ = "tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[int | None] = mapped_column(ForeignKey("components.id", ondelete="SET NULL"), nullable=True)
    test_key: Mapped[str] = mapped_column(String(400), index=True)
    name: Mapped[str] = mapped_column(Text)
    technique: Mapped[str] = mapped_column(String(60), default="smoke")
    category: Mapped[str] = mapped_column(String(40), default="functional", index=True)
    route: Mapped[str] = mapped_column(String(300), default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    expected: Mapped[str] = mapped_column(Text, default="")
    observed: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pass", index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    reproduced_n: Mapped[int] = mapped_column(Integer, default=0)
    reproduced_m: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    test_id: Mapped[int | None] = mapped_column(ForeignKey("tests.id", ondelete="SET NULL"), nullable=True)
    component_id: Mapped[int | None] = mapped_column(ForeignKey("components.id", ondelete="SET NULL"), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    route: Mapped[str] = mapped_column(String(300), default="", index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    expected: Mapped[str] = mapped_column(Text, default="")
    observed: Mapped[str] = mapped_column(Text, default="")
    standard_ref: Mapped[str] = mapped_column(String(120), default="")
    viewport: Mapped[str] = mapped_column(String(20), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    reproduced_n: Mapped[int] = mapped_column(Integer, default=0)
    reproduced_m: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="reported", index=True)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ReviewLabel(Base):
    __tablename__ = "review_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(20))  # true_positive | false_positive
    note: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
