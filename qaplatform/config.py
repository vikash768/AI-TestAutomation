"""Runtime settings, read once from the environment.

Everything the pipeline needs to know that is not part of a run request lives
here: database location, artifact folder, LLM model, browser limits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# name, width, height. The five widths the responsive checks run at.
DEFAULT_VIEWPORTS: list[tuple[str, int, int]] = [
    ("mobile", 360, 780),
    ("tablet", 768, 1024),
    ("laptop", 1024, 768),
    ("desktop", 1440, 900),
    ("wide", 1920, 1080),
]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    database_url: str = "postgresql+psycopg://qa:qa@localhost:5434/qa"
    artifacts_dir: Path = Path("artifacts")
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    llm_intent: bool = True
    github_token: str | None = None
    headless: bool = True
    max_pages: int = 60
    max_elements_per_page: int = 150
    nav_timeout_ms: int = 20000
    action_timeout_ms: int = 5000
    settle_ms: int = 1200
    worker_poll_seconds: float = 2.0
    secret_key: str = "change-me"
    public_base_url: str = "http://localhost:8000"
    viewports: list[tuple[str, int, int]] = field(default_factory=lambda: list(DEFAULT_VIEWPORTS))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("DATABASE_URL") or cls.database_url,
            artifacts_dir=Path(os.environ.get("ARTIFACTS_DIR") or "artifacts"),
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            gemini_model=os.environ.get("GEMINI_MODEL") or cls.gemini_model,
            gemini_fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL") or cls.gemini_fallback_model,
            llm_intent=_bool("LLM_INTENT", True),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            headless=_bool("HEADLESS", True),
            max_pages=_int("MAX_PAGES", 60),
            max_elements_per_page=_int("MAX_ELEMENTS_PER_PAGE", 150),
            nav_timeout_ms=_int("NAV_TIMEOUT_MS", 20000),
            action_timeout_ms=_int("ACTION_TIMEOUT_MS", 5000),
            settle_ms=_int("SETTLE_MS", 1200),
            worker_poll_seconds=float(os.environ.get("WORKER_POLL_SECONDS") or 2.0),
            secret_key=os.environ.get("QA_SECRET_KEY") or "change-me",
            public_base_url=(os.environ.get("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/"),
        )
