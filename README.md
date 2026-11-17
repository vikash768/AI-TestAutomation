# AI-TestAutomation

Give it a codebase and a live URL (web) or an app build (Android, iOS, macOS desktop). It reads the code to learn what every interactive component is supposed to do, explores the running application to see what each one actually does, tests the difference, and reports findings with evidence in Grafana. It never changes the application under test.

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
open http://localhost:8000      # submit a run: repo URL + live URL + test account
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

## Native apps (Android, iOS, macOS)

Pick the platform in the form, give the codebase (git URL or local path, plus a token for private repos; one per line: the app first, then optionally its backend API) and the build: a file path (`.apk`, `.ipa`, `.app`, `.dmg`) or the id of an app already installed on the device (`com.example.app`). The test account fields are used when the app opens on a sign-in screen.

| Stage | What happens on a device |
|---|---|
| 1 ingest | Clones the repo. React Native: screens from React Navigation (`<Stack.Screen name component>`) or Expo Router (`app/`), every Touchable / Pressable / Button / TextInput / Switch or component given `onPress`, with its label (`accessibilityLabel`, `title`, child `<Text>`), `testID`, navigation target, `Alert.alert` confirmation, API writes, `secureTextEntry` / `maxLength`. Controls rendered from data (`[{ title: 'Settings', route: 'Settings' }]`, `{ label, onPress: () => navigation.navigate(...) }`) count too. Electron: every menu template item grouped by its menu. The build is inspected (package / bundle id, launch activity, version, SHA-256, React Native / Flutter / Electron). A repo that cannot be cloned is reported as a warning and the build is still tested. **Build checks** (Android APK, no device): debuggable, signed with the debug key, `allowBackup`, cleartext HTTP, target SDK below the Play requirement, exported components without a permission, secrets and `http://` URLs in the JS bundle. **Code checks**: navigation to a screen the navigator does not register. |
| 2 explore | Installs or launches the build (Appium starts on demand, an emulator boots if no device is connected). Signs in when the first screen is a sign-in form. Walks the screens depth-first and taps every control classed safe; each tap records what happened: another screen, nothing, a crash, an ANR, fatal or JavaScript errors in logcat, another app in front. On macOS each application menu is opened and its safe items chosen. |
| 3 reconcile | Each observed screen is named after the code screen whose labels and texts it shows; the lead's matcher pairs code controls with live ones (accessibility label, text, `testID` = resource-id). |
| 4 execute | Per screen: blank screen, `undefined` / `NaN` / `[object Object]` shown to the user, controls with no accessible name or with code as their name, touch targets below 24 dp, overlapping or clipped controls, unmasked password fields. Per tap: crash, ANR, logged errors, the code's navigation target not reached. On the device: cold start time, sign-in negatives (empty, one wrong password, masking, account enumeration), rotation, background and resume, relaunch keeps the session, offline (Android), 300-character input into each reachable field, frame rendering (janky frames, `dumpsys gfxinfo`; held for review on an emulator) and memory (`dumpsys meminfo`). |
| 5 judge | Every crash, ANR or error tap is reproduced twice from a fresh launch; heuristic findings start as `needs_review`. |
| 6 report | Same Postgres tables, `report.html` and Grafana dashboards as web runs (`runs.platform` tells them apart). |

Every app finding says **where to fix it** (`app code`, `app build`, `backend`), **how** (in React Native / Android terms), **which file and line** (linked to the tested commit in the **QA Bug Report** dashboard), and the **steps to reproduce** from launch, next to its screenshot.

**Journeys with QA test data** (on by default; form: "Full journeys with QA test data"). Every screen with text fields and a submit control gets a create journey: fill the fields with `qa_<run>_<HHMMSS>` values, submit, check the screen the code navigates to. Boundary cases come from the code's own rules (`maxLength`, `required`). With `GEMINI_API_KEY` the LLM reads the inventory, form screens' source, what the explorer saw and the backend routes, and adds screen intent, expected behaviour per control and further journeys (verify, relaunch, edit, delete, validation). The LLM never acts: every planned step goes through the same safety rules, which are authoritative:

- A step that would send QA data to the backend, directly or because the screen it opens stores a record that the app uploads in the background, ends the journey as **blocked** unless "Let journeys create QA records on the backend" is ticked. The LLM can make a step's effect stricter, never looser.
- Bulk actions (sync all, upload all, clear) and sign-out are never used. Delete is used only in a row or screen that shows this run's own value.
- Screens that need hardware (reader, Bluetooth, USB) end the journey as **blocked**, with the screen's words as the reason.
- When the app refuses the QA value for its format (e.g. letters, digits and dashes only), the journey retries with `QA-<run>-<time>`.
- Everything the run creates is in `qa_data.json` (written after each change). At the end, and from `finally` after a failure, overwritten fields get their earlier value back and records are deleted where the app allows; the rest is listed as a leftover in the report.
- Expectations only the LLM supplied end as `needs_review` when they are not met. The LLM also reviews each journey's observed steps; anything it flags is `needs_review` only.

**Backend API load test** (off by default; form checkbox or `LOAD_TEST_ENABLED=true`). HTTP from this machine to the backend, never Appium sessions. GET only; endpoints with ids, behind sign-in (without `LOAD_TEST_BEARER_TOKEN`), or answering 401 / 403 / 404 to a first probe are skipped with the reason. Target: `LOAD_TEST_BASE_URL` (or the form field), else the origin the app's code names. Limits: `LOAD_TEST_USERS` (10), `LOAD_TEST_MAX_RPS` (20, shared by all users), `LOAD_TEST_DURATION_SECONDS` (30), hard caps 50 users / 100 req/s / 300 s. Reported per endpoint: requests, failures, error rate, req/s, p50 / p95 / p99 / max; error rate above `LOAD_TEST_MAX_ERROR_RATE` or p95 above `LOAD_TEST_P95_MS` is a finding. Device performance (cold start, rendering, memory) is a separate section of the report.

Safety on a device, where writes cannot be intercepted:

- Only controls classed **safe** from their label and from the code are tapped. Anything whose label or handler saves, submits, uploads, syncs, captures, deletes, pays, signs out or leaves the app (Support, Share, Hide, Quit) is held back and listed as skipped. A code handler that POSTs makes the control unsafe even when its label looks harmless.
- The explorer confirms nothing; its typed test input is never submitted, and the field's original value is put back. Journeys submit only forms that hold this run's QA values (see above). PIN pads are never typed into; optional setup after sign-in ("Set a PIN") is skipped, never completed.
- App data is kept: "Clean start" (clear the app's data first) is off by default, because it deletes anything stored only on the device (e.g. results not yet uploaded).
- macOS: the run stays inside the app. The system Apple menu (Sleep, Restart, Shut Down) is never read or clicked. The run moves the mouse and types on this Mac, so leave it alone while it runs.
- Network is turned back on and the device rotated back to portrait after every check, even when the check fails.

Requirements on the host: Node + `npm i -g appium`, then `appium driver install uiautomator2` (Android) and/or `appium driver install mac2` (macOS; grant Accessibility to the Appium/WebDriverAgent runner); Android SDK platform-tools and an emulator or a device with USB debugging. macOS may block reading builds in `~/Downloads` or `~/Desktop` from a terminal without Files and Folders access; grant it, or copy the build to `artifacts/builds/`.

```bash
make run-android REPO=https://github.com/org/mobile.git APP=/path/app-release.apk
make run-desktop REPO=https://github.com/jgraph/drawio-desktop APP=/Applications/draw.io.app
```

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