"""Claims queued runs and executes them one at a time.

QA_WORKER_PLATFORMS limits which runs this worker claims. The worker in
docker compose takes web runs; native app runs (android, ios, macos) need the
host's Appium server, devices and build files, so `make app-worker` runs a
worker for them on the host.
"""
from __future__ import annotations

import logging
import signal
import time

from sqlalchemy import select, update

from qaplatform.config import Settings
from qaplatform.db.models import Run, RunEvent
from qaplatform.db.session import init_db, session
from qaplatform.heartbeat import live_runs, start_heartbeat
from qaplatform.pipeline import run_pipeline

APP_PLATFORMS = ("android", "ios", "macos")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


def claim_next(database_url: str, platforms: tuple[str, ...] = ("web", *APP_PLATFORMS)) -> tuple[str, str] | None:
    with session(database_url) as db:
        row = db.execute(select(Run.id, Run.platform).where(Run.status == "queued", Run.platform.in_(platforms)).order_by(Run.created_at).limit(1)).first()
        if row is None:
            return None
        candidate, platform = row
        res = db.execute(update(Run).where(Run.id == candidate, Run.status == "queued").values(status="claimed", stage="claimed"))
        db.commit()
        return (candidate, platform) if res.rowcount == 1 else None


def requeue_interrupted(database_url: str, platforms: tuple[str, ...], artifacts_dir) -> list[str]:
    """Runs left claimed or running by a worker that stopped go back to the queue (their credentials are only wiped when a run ends)."""
    busy = live_runs(artifacts_dir)
    out = []
    with session(database_url) as db:
        for run in db.execute(select(Run).where(Run.status.in_(("claimed", "running")), Run.platform.in_(platforms))).scalars():
            if run.id in busy:
                continue
            run.status, run.stage, run.progress, run.message = "queued", "queued", 0, "re-queued: the worker running it stopped"
            db.add(RunEvent(run_id=run.id, level="warn", stage="queued", message="The worker running this run stopped; re-queued to start again"))
            out.append(run.id)
        db.commit()
    return out


def execute(run_id: str, platform: str, settings: Settings) -> None:
    if platform in APP_PLATFORMS:
        from qaplatform.app.pipeline import run_app_pipeline  # Appium client is only needed here

        run_app_pipeline(run_id, settings)
    else:
        run_pipeline(run_id, settings)


def _stop(signum, frame) -> None:
    raise SystemExit(0)


def main() -> None:
    settings = Settings.from_env()
    init_db(settings.database_url)
    logger.info("worker started for %s; polling %s every %.1fs", ",".join(settings.worker_platforms), settings.database_url.split("@")[-1], settings.worker_poll_seconds)
    # stop (make app-worker-stop, docker stop) through the pipelines' finally blocks: the device session ends and the network is restored
    signal.signal(signal.SIGTERM, _stop)
    state = start_heartbeat(settings.artifacts_dir, settings.worker_platforms)
    next_sweep = 0.0
    while True:
        claimed = None
        if time.time() >= next_sweep:  # at start and then while idle: runs left behind by a worker that stopped
            next_sweep = time.time() + 30
            try:
                for run_id in requeue_interrupted(settings.database_url, settings.worker_platforms, settings.artifacts_dir):
                    logger.warning("re-queued run %s: the worker that ran it stopped", run_id)
            except Exception as exc:
                logger.warning("re-queue sweep failed: %s", exc)
        try:
            claimed = claim_next(settings.database_url, settings.worker_platforms)
        except Exception as exc:
            logger.warning("claim failed: %s", exc)
        if claimed:
            run_id, platform = claimed
            logger.info("claimed %s run %s", platform, run_id)
            state["run"] = run_id
            execute(run_id, platform, settings)
            state["run"] = None
        else:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
