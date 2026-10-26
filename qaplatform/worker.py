"""Claims queued runs and executes them one at a time."""
from __future__ import annotations

import logging
import time

from sqlalchemy import select, update

from qaplatform.config import Settings
from qaplatform.db.models import Run
from qaplatform.db.session import init_db, session
from qaplatform.pipeline import run_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


def claim_next(database_url: str) -> str | None:
    with session(database_url) as db:
        candidate = db.execute(select(Run.id).where(Run.status == "queued").order_by(Run.created_at).limit(1)).scalar_one_or_none()
        if candidate is None:
            return None
        res = db.execute(update(Run).where(Run.id == candidate, Run.status == "queued").values(status="claimed", stage="claimed"))
        db.commit()
        return candidate if res.rowcount == 1 else None


def main() -> None:
    settings = Settings.from_env()
    init_db(settings.database_url)
    logger.info("worker started; polling %s every %.1fs", settings.database_url.split("@")[-1], settings.worker_poll_seconds)
    while True:
        run_id = None
        try:
            run_id = claim_next(settings.database_url)
        except Exception as exc:
            logger.warning("claim failed: %s", exc)
        if run_id:
            logger.info("claimed run %s", run_id)
            run_pipeline(run_id, settings)
        else:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
