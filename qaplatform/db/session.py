from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from qaplatform.db.models import Base

_engine: Engine | None = None
_factory: sessionmaker | None = None


def get_engine(database_url: str) -> Engine:
    global _engine, _factory
    if _engine is None or str(_engine.url) != database_url:
        kwargs = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_engine(database_url, future=True, **kwargs)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db(database_url: str) -> Engine:
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


# Columns added after the first release. create_all() does not alter existing
# tables, so a database created before them gets them here (idempotent).
_ADDED_COLUMNS = {
    "runs": {"platform": "VARCHAR(20) NOT NULL DEFAULT 'web'"},
    "findings": {"fix_in": "VARCHAR(30) NOT NULL DEFAULT ''", "fix": "TEXT NOT NULL DEFAULT ''", "code_ref": "TEXT NOT NULL DEFAULT ''", "steps": "JSON"},
}


def _add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def session(database_url: str) -> Session:
    get_engine(database_url)
    assert _factory is not None
    return _factory()
