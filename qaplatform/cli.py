"""Run the pipeline from a terminal without the web UI or the worker.

    python -m qaplatform.cli run --repo <git url or path> --live <url> [--project x] [--max-pages 60]

Credentials come from VALID_USER_EMAIL / VALID_USER_PASSWORD (or --email/--password).
"""
from __future__ import annotations

import argparse
import os
import sys

from qaplatform.config import Settings
from qaplatform.db.models import Run
from qaplatform.db.session import init_db, session
from qaplatform.pipeline import run_pipeline
from qaplatform.secrets import encrypt_secrets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qaplatform")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="create a run and execute it now")
    r.add_argument("--repo", default=os.environ.get("TARGET_REPO_URL", ""))
    r.add_argument("--live", default=os.environ.get("TARGET_LIVE_URL", os.environ.get("BASE_URL", "")))
    r.add_argument("--project", default=os.environ.get("PROJECT_NAME", "default"))
    r.add_argument("--email", default=os.environ.get("VALID_USER_EMAIL", ""))
    r.add_argument("--password", default=os.environ.get("VALID_USER_PASSWORD", ""))
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-phase2", action="store_true", help="smoke stage only (no reveal / forms / state / lifecycle / faults)")
    r.add_argument("--modules", default="", help="comma list of phase 2 modules to run (default: all)")
    r.add_argument("--branch", default="")
    sub.add_parser("initdb", help="create tables")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    init_db(settings.database_url)
    if args.cmd == "initdb":
        print("tables ready at", settings.database_url.split("@")[-1])
        return 0
    if not args.repo or not args.live:
        print("--repo and --live are required (or TARGET_REPO_URL / TARGET_LIVE_URL)", file=sys.stderr)
        return 2
    with session(settings.database_url) as db:
        run = Run(project=args.project, repo_url=args.repo, live_url=args.live, status="claimed", stage="claimed",
                  options={"max_pages": args.max_pages or settings.max_pages, "llm_intent": not args.no_llm, "phase2": not args.no_phase2,
                           "modules": [m.strip() for m in args.modules.split(",") if m.strip()] or None, "branch": args.branch},
                  secrets=encrypt_secrets(settings.secret_key, {"email": args.email, "password": args.password}))
        db.add(run)
        db.commit()
        run_id = run.id
    print("run", run_id)
    run_pipeline(run_id, settings)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        print("status:", run.status, "|", run.message)
        if run.error:
            print(run.error)
        print("report:", settings.artifacts_dir / "runs" / run_id / "report.html")
        return 0 if run.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
