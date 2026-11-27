"""Run the pipeline from a terminal without the web UI or the worker.

    python -m qaplatform.cli run --repo <git url or path> --live <url> [--project x] [--max-pages 60]
    python -m qaplatform.cli run --platform android --app <path.apk | package id> --project <name>
    python -m qaplatform.cli run --platform macos --app <path.app | path.dmg | bundle id> [--repo <git url or path>]

Credentials come from VALID_USER_EMAIL / VALID_USER_PASSWORD (or --email/--password).
Supply repository URLs and private repository tokens per run with --repo and --github-token.
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
    r.add_argument("--repo", default="", help="repository URL or local checkout for this run")
    r.add_argument("--platform", default="web", choices=["web", "android", "ios", "macos"])
    r.add_argument("--live", default=os.environ.get("TARGET_LIVE_URL", os.environ.get("BASE_URL", "")))
    r.add_argument("--app", default="", help="native runs: build file (.apk/.ipa/.app/.dmg) or installed app id")
    r.add_argument("--clean-start", action="store_true", help="native runs: clear the app's data on the device first")
    r.add_argument("--project", default="", help="project name for this run, required for Android")
    r.add_argument("--email", default=os.environ.get("VALID_USER_EMAIL", ""))
    r.add_argument("--password", default=os.environ.get("VALID_USER_PASSWORD", ""))
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-phase2", action="store_true", help="smoke stage only (no reveal / forms / state / lifecycle / faults)")
    r.add_argument("--modules", default="", help="comma list of phase 2 modules to run (default: all)")
    r.add_argument("--branch", default="")
    r.add_argument("--github-token", default="", help="private repository access token for this run")
    sub.add_parser("initdb", help="create tables")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "initdb":
        init_db(settings.database_url)
        print("tables ready at", settings.database_url.split("@")[-1])
        return 0
    target = (args.live if args.platform == "web" else args.app).strip()
    project = args.project.strip()
    if args.platform == "web" and not (args.repo and target):
        print("--repo and a live URL are required (set --live or TARGET_LIVE_URL)", file=sys.stderr)
        return 2
    if args.platform != "web" and not target:
        print("--app is required for native runs", file=sys.stderr)
        return 2
    if args.platform == "android" and not project:
        print("--project is required for Android runs", file=sys.stderr)
        return 2
    default_max_pages = {"web": settings.max_pages, "macos": settings.macos_max_screens}.get(args.platform, settings.max_screens)
    init_db(settings.database_url)
    with session(settings.database_url) as db:
        run = Run(project=project or "default", platform=args.platform, repo_url=args.repo, live_url=target, status="claimed", stage="claimed",
                  options={"max_pages": args.max_pages or default_max_pages, "llm_intent": not args.no_llm, "phase2": not args.no_phase2,
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
