"""Run the pipeline from a terminal without the web UI or the worker.

    python -m qaplatform.cli run --repo <git url or path> --live <url> [--project x] [--max-pages 60]
    python -m qaplatform.cli run --platform android --repo <git url or path> --app <path.apk | package id>
    python -m qaplatform.cli run --platform macos --repo <git url or path> --app <path.app | path.dmg | bundle id>

Credentials come from VALID_USER_EMAIL / VALID_USER_PASSWORD (or --email/--password).
"""
from __future__ import annotations

import argparse
import os
import sys

from qaplatform.config import Settings
from qaplatform.db.models import Run
from qaplatform.db.session import init_db, session
from qaplatform.heartbeat import start_heartbeat
from qaplatform.secrets import encrypt_secrets
from qaplatform.worker import execute


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qaplatform")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="create a run and execute it now")
    r.add_argument("--repo", default=os.environ.get("TARGET_REPO_URL", ""))
    r.add_argument("--platform", default="web", choices=["web", "android", "ios", "macos"])
    r.add_argument("--live", default=os.environ.get("TARGET_LIVE_URL", os.environ.get("BASE_URL", "")))
    r.add_argument("--app", default=os.environ.get("APP_BUILD_PATH", ""), help="native runs: build file (.apk/.ipa/.app/.dmg) or installed app id")
    r.add_argument("--clean-start", action="store_true", help="native runs: clear the app's data on the device first")
    r.add_argument("--project", default=os.environ.get("PROJECT_NAME", "default"))
    r.add_argument("--email", default=os.environ.get("VALID_USER_EMAIL", ""))
    r.add_argument("--password", default=os.environ.get("VALID_USER_PASSWORD", ""))
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-phase2", action="store_true", help="smoke stage only (no reveal / forms / state / lifecycle / faults)")
    r.add_argument("--modules", default="", help="comma list of phase 2 modules to run (default: all)")
    r.add_argument("--branch", default="")
    r.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""))
    sub.add_parser("initdb", help="create tables")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    init_db(settings.database_url)
    if args.cmd == "initdb":
        print("tables ready at", settings.database_url.split("@")[-1])
        return 0
    target = args.live if args.platform == "web" else args.app
    if args.platform == "web" and not (args.repo and target):
        print("--repo and --live are required (or TARGET_REPO_URL / TARGET_LIVE_URL)", file=sys.stderr)
        return 2
    if args.platform != "web" and not target:
        print("--app is required for native runs (or APP_BUILD_PATH)", file=sys.stderr)
        return 2
    with session(settings.database_url) as db:
        run = Run(project=args.project, platform=args.platform, repo_url=args.repo, live_url=target, status="claimed", stage="claimed",
                  options={"max_pages": args.max_pages or (settings.max_pages if args.platform == "web" else settings.max_screens), "llm_intent": not args.no_llm, "phase2": not args.no_phase2,
                           "modules": [m.strip() for m in args.modules.split(",") if m.strip()] or None, "branch": args.branch, "clean_start": args.clean_start},
                  secrets=encrypt_secrets(settings.secret_key, {"email": args.email, "password": args.password, "github_token": args.github_token}))
        db.add(run)
        db.commit()
        run_id = run.id
    print("run", run_id)
    # tell running workers this run is taken, or their sweep would re-queue it and start it a second time
    start_heartbeat(settings.artifacts_dir, ())["run"] = run_id
    execute(run_id, args.platform, settings)
    with session(settings.database_url) as db:
        run = db.get(Run, run_id)
        print("status:", run.status, "|", run.message)
        if run.error:
            print(run.error)
        print("report:", settings.artifacts_dir / "runs" / run_id / "report.html")
        return 0 if run.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
