# AI-TestAutomation

Give it a codebase and a live URL. It reads the code to learn what every interactive component is supposed to do, crawls the live application to see what each one actually does, tests the difference, and reports findings with evidence in Grafana. It never changes the application under test.

```
codebase URL ─┐                                  ┌─ Postgres ──┐
              ├─▶ ingest ▶ explore ▶ reconcile ▶ execute ▶ judge ▶ report ─┤             ├─▶ Grafana (the report)
live URL ─────┘            (Playwright, 5 viewports)                        └─ Prometheus ┘
```

## Quick start

```bash
cp .env.example .env            # set GEMINI_API_KEY, the test account, QA_SECRET_KEY
make up                         # builds the image, starts Postgres, API, worker, Prometheus, Grafana
open http://localhost:8000      # submit a run: repo URL + live URL + test account
open http://localhost:3000      # Grafana (admin/admin): QA Runs, QA Findings, QA Components
```

Submitting the form queues a run; the worker picks it up within seconds. The run page shows the live log, and when it finishes you get the HTML report, the findings table with review buttons, and links into Grafana.

Run from a terminal instead of the form:

```bash
make run REPO=https://github.com/org/frontend.git LIVE=https://app.example.com
# or locally, outside Docker:
set -a; . ./.env; set +a
.venv/bin/python -m qaplatform.cli run --repo <git url or path> --live <url> --project <name>
```

## What a run does

| Stage | What happens | Output |
|---|---|---|
| 1 ingest | Clones the repo (anonymously, then with `GITHUB_TOKEN` if needed), detects the framework, parses every JS/JSX/TS/TSX file with tree-sitter. Extracts routes and their guards, every interactive element with its label, handler, navigation target, drawer/confirm behaviour and API calls, timers, storage keys. Wrapper components (`Button`, `Field`, `DropdownMenu`) resolve to the native control they render. Gemini summarises each page file into one-line expectations, persistence and lifecycle rules (source code only, cached by file hash). | `inventory.json`, intent per component |
| 2 explore | Playwright. Unauthenticated pass: does every protected route send us to login? Then logs in with the test account and crawls breadth-first from the code's routes plus discovered links. Per page: interactive elements with stable selectors, console and network monitors, screenshots and a layout check at 360 / 768 / 1024 / 1440 / 1920 px. | `explore.json`, screenshots |
| 3 reconcile | Matches code elements to observed elements by route, control type, label, href and id. Produces the component matrix: found, not found, present but undeclared. | coverage % |
| 4 execute | Route-guard tests, page health (HTTP status, blank render, console errors, failed requests, slow load), responsive checks (horizontal overflow, overlapping controls, tap targets under 24 px, clipped text), and a smoke click on every safe component with the code's expectation as the oracle (link target, drawer opens, nothing breaks). Logout is tested last with its own oracle. | tests |
| 4b reveal | Clicks every control that opened or changed something, lists the controls that appear (menu items, drawer forms, tab panels, date pickers), matches them to the code's conditional components and tests them, two levels deep. Each opened panel is checked for dialog role, focus and Escape (WAI-ARIA dialog pattern). | coverage after interaction |
| 4c destructive | Destructive controls whose code opens a confirmation are clicked, the confirmation is checked, then cancelled. Nothing is ever confirmed. A destructive API call with no confirmation in the code is reported for review without clicking. | confirm tests |
| 4d forms | Sign-in negative tests (masking, empty submit, unknown account, wording that reveals accounts). Every other form: invalid partitions and boundaries from its constraints (required, email, min/max, minLength) must send nothing; a valid submit must send a write; the failed write must be reported to the user. | validation, wiring, feedback |
| 4e state | Reload keeps the user signed in on the same route; a new tab shares a localStorage session; Back/Forward return to the right page; logout with the server unreachable still clears local credentials; Back after logout shows sign-in; unsaved edits (review). | persistence tests |
| 4f lifecycle | Every `setInterval` with a period from the code is checked with a controlled clock: it polls at that period, an "Auto-refresh" control stops it, leaving the page stops it. Timers that sign the user out are checked to do so. | timer tests |
| 4g faults | Each page's own data reads are failed (HTTP 502, offline, slow): the page must not go blank, must say what happened, Retry must recover, and a pending load must not claim there is no data. | fault tests |
| 5 judge | Every click failure is re-run twice. Confidence comes from reproduction; heuristic and code-only findings start as `needs_review`; anything below the threshold is kept, never dropped. | confidence, status |
| 6 report | Writes pages, components, tests and findings to Postgres, renders `report.html`, wipes the run's credentials and browser session. | Grafana, report |

Finding record: severity, category (functional, stability, security, ui-responsive, accessibility, performance), route, viewport, title, expected, observed, standard reference (OWASP WSTG, WCAG 2.2, ISO/IEC 25010, Nielsen), confidence, reproduced n/m, evidence (screenshot, console, requests).

## Safety rules

- Report only. Nothing in the application under test is modified by the platform.
- **Write guard.** Every browser context that clicks, types or submits runs behind a request interceptor: any POST, PUT, PATCH or DELETE is recorded (method, path, field names; never values) and answered in the browser with a blocked error, so it never reaches the server. sendBeacon and websockets are switched off. Only token refresh passes, plus the real sign-ins and the one real logout test. The seeded-bug benchmark asserts that no other write reaches its server.
- Because writes are intercepted, Save buttons and forms are exercised: what they would send is the test result.
- **Destructive** controls (delete, remove, purge, clean, deactivate, reset, archive, revoke) are never confirmed. When the code shows a confirmation step, the confirmation is opened and cancelled; otherwise the control is not clicked at all.
- Sign-in tests make at most one failed attempt on the real account (and one on an invented address).
- Credentials are encrypted at rest with a key derived from `QA_SECRET_KEY`, decrypted only inside the worker, and deleted when the run ends. They are never sent to the LLM.
- Security checks are passive observations (route guards, session end, unauthorized responses). No load or fuzzing is generated against the target.

## Accuracy

The goal is precision at or above 0.90: at least nine of ten reported findings are real. Each finding on the run page has **real** / **not a bug** buttons; those labels feed `qa_latest_precision_ratio` and the gauge on the QA Runs dashboard. Coverage (code components found on the live app) is reported both overall and for always-rendered components, because elements inside drawers, menus and conditional branches are legitimately absent at rest.

## Layout

```
qaplatform/
  config.py            settings from the environment
  cli.py               run a pipeline from a terminal
  worker.py            claims queued runs and executes them
  pipeline.py          the seven stages
  api/                 FastAPI UI + JSON + /metrics, Jinja templates
  db/                  SQLAlchemy models (runs, pages, components, tests, findings, review_labels)
  ingest/              repo fetch, tree-sitter parser, intent builder (+ optional Gemini enrichment)
  explore/             Playwright session, DOM scripts, crawler, action-safety classifier
  reconcile/           code <-> live matcher
  execute/             write guard, smoke runner, reveal, destructive, forms (+ sign-in), state, lifecycle, faults
  judge/               confidence gate
  report/              DB writer, Prometheus collector, HTML report
observability/
  grafana/             datasources (Postgres + Prometheus), dashboards qa-runs / qa-findings / qa-components
  prometheus/          scrapes api:8000/metrics
tests/unit/            parser, safety, matcher, guard, browser scripts, executor, API, offline pipeline, seeded-bug benchmark
tests/fixture/         fixture SPA with planted bugs, served inside the browser (no sockets)
docs/                  CURRENT_STATE.md (audit of the previous code), TARGET_ARCHITECTURE.md, BASELINE_TRACKPILOT.md, legacy/
artifacts/runs/<id>/   report.html, inventory.json, explore.json, screenshots/
```

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
docker compose up -d postgres
make test                                  # unit tests (headless Chromium for the browser ones)
TRACKPILOT_FE_PATH=/path/to/trackpilot_FE make test   # also parses the real TrackPilot repo
```

## Services and ports

| Service | Port | Purpose |
|---|---|---|
| api | 8000 | Web UI, JSON API, `/metrics`, `/artifacts/*` (screenshots, reports) |
| worker | - | Runs pipelines with headless Chromium |
| postgres | 5434 (host) | Findings database; Grafana's datasource |
| prometheus | 9090 | Run-level gauges and trends |
| grafana | 3000 | The report: QA Runs, QA Findings, QA Components |

## Roadmap

Phase 1: every interactive component known from the code, found on the live app, smoke-tested, with route-guard, page-health and responsive checks, reported in Grafana.
Phase 2 (this release): interaction coverage (menus, drawers, tabs), destructive-confirmation checks, form validation / wiring / failure feedback, sign-in negative tests, persistence and navigation, clock-controlled timers, fault injection, all under the write guard; seeded-bug benchmark.
Phase 3: WCAG 2.2 AA via axe-core, OWASP ZAP baseline, UX heuristics with evidence-citing LLM judgment.
Phase 4: threshold calibration from review labels, second application, report polish.
