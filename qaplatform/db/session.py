from __future__ import annotations

from sqlalchemy import create_engine
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


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration for columns added after a table already exists (no Alembic here).
    create_all never ALTERs an existing table, so add new columns idempotently."""
    from sqlalchemy import text
    adds = [("findings", "bucket", "VARCHAR(10) DEFAULT 'problem'")]
    for table, col, decl in adds:
        with engine.begin() as conn:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))
            except Exception:
                pass  # already exists (or table absent on a brand-new DB that create_all just made correctly)


def session(database_url: str) -> Session:
    get_engine(database_url)
    assert _factory is not None
    return _factory()
