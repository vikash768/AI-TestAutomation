# AI-TestAutomation

Run web QA with a codebase and live URL. Android and macOS can test an app, review a backend repository, or do both in one run. iOS supports source-assisted device testing. Findings and evidence appear in the run report and Grafana.

Web runs use Playwright. Native app runs use Appium (UiAutomator2 for Android, XCUITest for iOS, Mac2 for macOS) through the same stages, tables and dashboards; see [Native apps](#native-apps-android-ios-macos).

```
codebase URL ─┐                                  ┌─ Postgres ──┐
              ├─▶ ingest ▶ explore ▶ reconcile ▶ execute ▶ judge ▶ report ─┤             ├─▶ Grafana (the report)
live URL ─────┘            (Playwright, 5 viewports)                        └─ Prometheus ┘
```

## Quick start

```bash
cp .env.example .env            # set GEMINI_API_KEY, the test account, QA_SECRET_KEY
make up                         # builds the image, starts Postgres, API, worker, Prometheus, Grafana
open http://localhost:8000      # choose a platform and submit the inputs for your run
open http://localhost:3000      # Grafana (admin/admin): QA Bug Report, QA Runs, QA Findings, QA Components
```

For Android and desktop apps, also start the host worker (it drives Appium on this machine):

```bash
make app-worker-start           # claims android / ios / macos runs in the background; starts Appium if none is running
make app-worker-status          # is it running? (make app-worker-stop to stop it; make app-worker runs it in the foreground)
```

A run whose platform has no worker stays queued; its run page says so and gives the command to start one. Each worker writes a heartbeat to `artifacts/workers/`; a run left behind by a worker that stopped goes back to the queue within 30 s and starts again.

Submitting the form queues a run; the worker picks it up within seconds. The run page shows the live log, and when it finishes you get the HTML report, the findings table with review buttons, and links into Grafana.

To start over, ` ` deletes every run (or `make reset-runs PROJECT=<name>` one project's) from the database with its findings, tests, review labels and artifacts. `make clean` removes only the artifact files and keeps the database.

Run from a terminal instead of the form:

```bash
make run REPO=https://github.com/org/frontend.git LIVE=https://app.example.com
# or locally, outside Docker:
set -a; . ./.env; set +a
.venv/bin/python -m qaplatform.cli run --repo <git url or path> --live <url> --project <name>
```

Enter a project name for each run. Supply a repository URL and token when required. On the CLI, use `--project`, `--repo` and `--github-token`.

## What a run does

| Stage | What happens | Output |
|---|---|---|
| 1 ingest | Clones the repo using the token submitted for the run if anonymous access fails, detects the framework, and parses JS/JSX/TS/TSX source with tree-sitter. Extracts routes, guards, interactive controls, handlers, navigation targets, API calls, timers, and storage keys. Wrapper components (`Button`, `Field`, `DropdownMenu`) resolve to the native control they render. Gemini can summarise expectations, persistence and lifecycle rules from source, with results cached by file hash. | `inventory.json`, intent per component |
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

## Native apps (Android, iOS, macOS)

Select **Android app (Appium)** or **macOS desktop app (Appium)**, choose a testing mode, and enter a project name. Select or enter the app for each Frontend and Full run.

| Mode | What to enter | What runs |
|---|---|---|
| **Frontend testing** | Android APK upload, APK path, or package ID. On macOS, choose an installed app or enter a `.app` path, `.dmg` path, or bundle ID. | Exercises the app through Appium and reports findings and screenshots when available. You can add an expected screen result. |
| **Backend testing** | The backend repository's HTTPS Git URL and a Git access token. | Reviews backend route declarations and authentication hints. You can verify an expected HTTP response from the deployed API. |
| **Full testing** | One app target plus the backend Git URL and token. | Runs both sets of checks and reports separate Frontend and Backend outcomes, including checks completed before the other side was blocked. |

Android and macOS Frontend and Full use the worker's configured exploration budgets. Their frontend source coverage is unavailable because they exercise the app directly. To add bounded GET performance checks in Backend or Full, enable **Backend API load test** and enter the deployed API base URL.

- **Expected app screen.** Enter the control labels in navigation order, one per line, and the text you expect at the destination. Leave the path empty for the first screen after sign-in if sign-in is needed. The report checks observed screen text without regard to case, includes the screenshot when captured, and marks a path as blocked when exploration did not reach it.
- **Expected API response.** Enter the deployed base URL and a GET path such as `/health`. The expected status defaults to `200`. Optionally enter a dotted JSON object field such as `service.ready` and its expected value, and supply a separate API bearer token for that request. Enter JSON literals like `true` or quoted strings like `"true"` to select the value and type. The report records the result without saving the response body or bearer token.
- **Combined expectations.** When both expectations are supplied in Full testing, the report shows their individual results and whether both passed. Choose expectations from the same test fixture when the two checks should describe the same workflow.

For iOS, enter an `.ipa` path or installed app ID. Provide each Git URL or local path on its own line, starting with the app and optionally its backend, and enter a Git token for a private repository. Device runs can use a test account when the app opens on a sign-in screen.

### macOS desktop testing

1. Install Xcode and select it as the active developer directory. Install a Mac2 driver compatible with your Appium major version. For Appium 3, use `appium driver install mac2@4.6.1`. Run `appium driver doctor mac2` on the Mac. [Mac2 setup and permissions](https://appium.github.io/appium-mac2-driver/v4/getting-started/)
2. Start the services with `make up` and the macOS host worker with `make app-worker-start`. The picker reads the App Catalog published by the host worker. It refreshes periodically and searches the standard system and user Applications folders.
3. In the web UI, choose **macOS desktop app (Appium)**, select a mode, and enter the project name. For Frontend and Full, select an installed app or enter a path or bundle ID manually. For Backend and Full, enter the Git URL and token. Choose **Start run** and open the HTML report from the run page.

Mac2 runs in the Mac's logged-in desktop session. Each local desktop run requests a fresh XCTest runner so macOS can authorize the new session. Keep the Mac unlocked and authenticate if **Enable Automation** appears. If a run stops while attaching to an open app, check the Xcode test log for denied UI testing access and review the Mac2 device preparation steps. Reports capture the tested window when available. Backend mode runs source and optional API checks through the host worker without opening an app. [Mac2 device preparation](https://appium.github.io/appium-mac2-driver/v4/getting-started/#device-preparation)

On a local Mac worker, Frontend and Full open the selected installed app or bundle and verify its process before Mac2 attaches. If the picker finds several copies with the same bundle ID, it shows each path. Launch errors identify an invalid code signature, an app that exits during startup, or a different copy that opened. Electron apps open in GUI mode and request renderer accessibility. An Electron process that was already open may keep its original launch settings. [Electron environment variables](https://www.electronjs.org/docs/latest/api/environment-variables#electron_run_as_node) · [Chromium renderer accessibility](https://www.chromium.org/developers/design-documents/accessibility/)

Desktop exploration walks accessible app windows, menus, tabs, disclosures, dialogs, and supported scroll areas. It exercises controls classified as safe and checks observed UI elements and eligible search or filter inputs. For readable checkboxes and switches, it verifies the state change and restores the initial setting. It opens dropdowns and records their choices. Choices that could change a saved setting are held back. Open **Desktop coverage** in the HTML report to see the counts by control type and reasons for missed controls. Desktop runs use `MACOS_MAX_SCREENS` and `MACOS_EXPLORATION_SECONDS`, which default to 120 discovered views and 900 seconds of exploration. A coverage row marked **Blocked** means the explorer could only verify part of the app UI.

When a local desktop run finishes, the worker closes the Appium session and shuts down the XCTest runner it started.

Start the local host worker before submitting. Android APK uploads accept up to 512 MiB. The installation preserves existing app data by default. Uploaded APKs are held privately while the run is queued or in progress and removed when it completes or fails. New Git checkouts use a temporary private directory outside the report tree and are cleaned up at the end of each worker attempt.

| Stage | What happens on a device |
|---|---|
| 1 ingest | Inspects package metadata and, when app source is provided, extracts React Native screens, controls, labels, handlers and navigation or Electron menus. For a supplied Android APK, checks manifest settings, signing and JavaScript bundle hygiene. In Android Backend and Full, a failed backend checkout blocks route inventory while other configured checks can continue. |
| 2 explore | Installs or launches the build (Appium starts on demand, an emulator boots if no device is connected). Signs in when the first screen is a sign-in form. It exercises controls classified as safe and records the response. On macOS, the explorer also records the app menus, nested views, and uncovered branches. |
| 3 reconcile | Records the screens and controls seen on the device. When app source is available, associates observed controls with the source inventory using accessibility labels, text and test IDs. |
| 4 execute | Per screen: blank screen, `undefined` / `NaN` / `[object Object]` shown to the user, controls with no accessible name or with code as their name, touch targets below 24 dp, overlapping or clipped controls, unmasked password fields. Per tap: crash, ANR, logged errors, the code's navigation target not reached. On the device: cold start time, sign-in negatives (empty, one wrong password, masking, account enumeration), rotation, background and resume, relaunch keeps the session, offline (Android), 300-character input into each reachable field, frame rendering (janky frames, `dumpsys gfxinfo`; held for review on an emulator) and memory (`dumpsys meminfo`). |
| 5 judge | Every crash, ANR or error tap is reproduced twice from a fresh launch; heuristic findings start as `needs_review`. |
| 6 report | Same Postgres tables, `report.html` and Grafana dashboards as web runs (`runs.platform` tells them apart). |

App findings include suggested fixes, source references when available, and reproduction steps and screenshots when recorded.

**Journeys with QA test data** are available on source-assisted iOS submissions in the web form and on compatible older source-assisted native runs. A create journey fills fields with `qa_<run>_<HHMMSS>` values, submits, and checks the expected next screen. Boundary cases use constraints in app source such as `maxLength` and `required`. Gemini can suggest additional screen expectations and journeys. The runner validates planned steps against the safety rules.

- A step that would send QA data to the backend, directly or because the screen it opens stores a record that the app uploads in the background, ends the journey as **blocked** unless "Let journeys create QA records on the backend" is ticked. The LLM can make a step's effect stricter, never looser.
- Bulk actions (sync all, upload all, clear) and sign-out are never used. Delete is used only in a row or screen that shows this run's own value.
- Screens that need hardware (reader, Bluetooth, USB) end the journey as **blocked**, with the screen's words as the reason.
- When the app refuses the QA value for its format (e.g. letters, digits and dashes only), the journey retries with `QA-<run>-<time>`.
- Everything the run creates is in `qa_data.json` (written after each change). At the end, and from `finally` after a failure, overwritten fields get their earlier value back and records are deleted where the app allows; the rest is listed as a leftover in the report.
- Expectations only the LLM supplied end as `needs_review` when they are not met. The LLM also reviews each journey's observed steps; anything it flags is `needs_review` only.

**Backend API load test** uses bounded GET requests from the host worker. Android and macOS Backend and Full require the run's checkbox and deployed base URL and use public endpoints. Source-assisted runs can use configured load-test defaults or a discovered origin. Reports include request counts, error rate, throughput, latency and endpoints that were skipped. The existing `LOAD_TEST_*` settings control limits and failure thresholds.

Safety on a device, where writes cannot be intercepted:

- Only controls classed **safe** from their label and from the code are tapped. Anything whose label or handler saves, submits, uploads, syncs, captures, deletes, pays, signs out or leaves the app (Support, Share, Hide, Quit) is held back and listed as skipped. A code handler that POSTs makes the control unsafe even when its label looks harmless.
- The explorer confirms nothing; its typed test input is never submitted, and the field's original value is put back. Journeys submit only forms that hold this run's QA values (see above). PIN pads are never typed into; optional setup after sign-in ("Set a PIN") is skipped, never completed.
- App data is kept: "Clean start" (clear the app's data first) is off by default, because it deletes anything stored only on the device (e.g. results not yet uploaded).
- macOS: the run stays inside the app. The system Apple menu (Sleep, Restart, Shut Down) is never read or clicked. The run moves the mouse and types on this Mac, so leave it alone while it runs.
- Network is turned back on and the device rotated back to portrait after every check, even when the check fails.

For Android, install Node, Appium and UiAutomator2, then configure Android SDK platform-tools and an emulator or a device with USB debugging. The macOS requirements are in the desktop setup above. For builds in protected macOS folders, grant the worker access or copy the build to `artifacts/builds/`.

```bash
make run-android APP=/path/app-release.apk PROJECT=my-android-app
make run-desktop APP=/Applications/draw.io.app PROJECT=desktop-qa
```

Direct Android CLI runs require `--app` and `--project`.

## Safety rules

- Report only. Nothing in the application under test is modified by the platform.
- **Write guard.** Every browser context that clicks, types or submits runs behind a request interceptor: any POST, PUT, PATCH or DELETE is recorded (method, path, field names; never values) and answered in the browser with a blocked error, so it never reaches the server. sendBeacon and websockets are switched off. Only token refresh passes, plus the real sign-ins and the one real logout test. The seeded-bug benchmark asserts that no other write reaches its server.
- Because writes are intercepted, Save buttons and forms are exercised: what they would send is the test result.
- **Destructive** controls (delete, remove, purge, clean, deactivate, reset, archive, revoke) are never confirmed. When the code shows a confirmation step, the confirmation is opened and cancelled; otherwise the control is not clicked at all.
- Sign-in tests make at most one failed attempt on the real account (and one on an invented address).
- Credentials are encrypted at rest with a key derived from `QA_SECRET_KEY`, decrypted only inside the worker, and deleted when the run ends. They are never sent to the LLM.
- Security checks are passive observations (route guards, session end, unauthorized responses). No fuzzing is generated against the target, and no load unless a native run opts into the bounded GET-only backend load test.

## Accuracy

The goal is precision at or above 0.90: at least nine of ten reported findings are real. Each finding on the run page has **real** / **not a bug** buttons; those labels feed `qa_latest_precision_ratio` and the gauge on the QA Runs dashboard. Coverage (code components found on the live app) is reported both overall and for always-rendered components, because elements inside drawers, menus and conditional branches are legitimately absent at rest.

## Layout

```
qaplatform/
  config.py            settings from the environment
  cli.py               run a pipeline from a terminal
  worker.py            claims queued runs (QA_WORKER_PLATFORMS) and executes them
  pipeline.py          the seven stages
  api/                 FastAPI UI + JSON + /metrics, Jinja templates
  db/                  SQLAlchemy models (runs, pages, components, tests, findings, review_labels)
  ingest/              repo fetch, tree-sitter parser, intent builder (+ optional Gemini enrichment)
  explore/             Playwright session, DOM scripts, crawler, action-safety classifier
  reconcile/           code <-> live matcher
  execute/             write guard, smoke runner, reveal, destructive, forms (+ sign-in), state, lifecycle, faults
  judge/               confidence gate
  report/              DB writer, Prometheus collector, HTML report
  app/                 native apps: build inspection, Appium session, RN / Electron code extraction, explorer, checks, pipeline
observability/
  grafana/             datasources (Postgres + Prometheus), dashboards qa-bugs (for developers) / qa-runs / qa-findings / qa-components
  prometheus/          scrapes api:8000/metrics
tests/unit/            parser, safety, matcher, guard, browser scripts, executor, API, offline pipeline, seeded-bug benchmark
tests/fixture/         fixture SPA with planted bugs (served inside the browser), React Native and Electron fixture apps
docs/                  CURRENT_STATE.md (audit of the previous code), TARGET_ARCHITECTURE.md, BASELINE_TRACKPILOT.md, legacy/ (superseded suite files and notes)
scripts/security-check.sh  tracked-secret / build / artifact hygiene check (make security-check)
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
| worker | - | Runs web pipelines with headless Chromium |
| app-worker (host, `make app-worker-start`) | - | Runs Android / iOS / macOS pipelines through Appium |
| appium (host) | 4723 | Started on demand by the app worker |
| postgres | 5434 (host) | Findings database; Grafana's datasource |
| prometheus | 9090 | Run-level gauges and trends |
| grafana | 3000 | The report: QA Bug Report (one row per bug: fix, code line, steps, screenshot), QA Runs, QA Findings, QA Components |

## Roadmap

Phase 1: every interactive component known from the code, found on the live app, smoke-tested, with route-guard, page-health and responsive checks, reported in Grafana.
Phase 2 (this release): interaction coverage (menus, drawers, tabs), destructive-confirmation checks, form validation / wiring / failure feedback, sign-in negative tests, persistence and navigation, clock-controlled timers, fault injection, all under the write guard; seeded-bug benchmark.
Phase 3: WCAG 2.2 AA via axe-core, OWASP ZAP baseline, UX heuristics with evidence-citing LLM judgment.
Phase 4: threshold calibration from review labels, second application, report polish.
3
