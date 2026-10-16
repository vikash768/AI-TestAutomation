# AI Testing Automation

AI-driven test automation platform that reads project-specific testing instructions and dynamically generates, executes, and reports test results.

Supports **web applications** (Selenium), **desktop applications** (Appium), and **mobile applications** (Appium — Android & iOS).

## Architecture

```
testsuites/*.testsuite.md            ← Testing instructions (source of truth)
        ↓
AI Suite Parser                       ← Parse markdown into structured context
        ↓
AI Test Generator                     ← Generate concrete test cases + actions
        ↓
Structured Actions                    ← CLICK / TYPE / VERIFY / TAP / SWIPE / etc.
        ↓
Execution Selection                   ← Route by TARGET_TYPE
        ↓
┌──────────────┬──────────────┐
│              │              │
Web          Desktop        Mobile
│              │              │
Selenium      Appium         Appium
Executor      Executor       Executor
        ↓
Validation + Evidence                 ← Expected vs Actual + screenshots
        ↓
Prometheus → Grafana                  ← Metrics + dashboards
```

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 2. Start infrastructure
make up

# 3. Verify services
make health

# 4. Run AI-driven tests (default: TrackPilot, web)
make test-ai

# 5. Run tests for a specific suite
make test-suite SUITE=testsuites/trackpilot.testsuite.md

# 6. View results in Grafana
open http://localhost:3000

# 7. Reset environment and wipe test results to run fresh tests
make clean
```

## Lead Demo

The demo commands select the correct suite and project automatically and run
platform preflight checks before test generation. They generate the first test
category so the browser/device appears quickly during a live presentation:

```bash
# TrackPilot web application through Selenium/Chrome
make demo-web

# IJustPaid Android application through Appium
# Starts the configured emulator/Appium when needed and resets local app data.
make demo-mobile

# Optional: demonstrate more categories
make demo-web AI_MAX_CATEGORIES=3 AI_MAX_TESTS=5
```

For the Selenium run, open `http://localhost:7900` (password: `secret`). Chrome
appears when the terminal reaches `[3/6] Executing tests...`; the Selenium Grid
wallpaper is expected while test cases are being generated or after they finish.
Use `make test-web` for the full Selenium suite. `make test-mobile` runs the
complete IJustPaid suite and requires both repository and APK inspection to
succeed; `make demo-mobile` is the fast APK-backed presentation path.

`make test-web` enforces one executable test for every instruction in the
Markdown suite. The current TrackPilot suite contains 11 pages, 68 categories,
and 525 instructions. A short AI plan is rejected instead of silently skipping
components, demo limits are cleared, and all generated cases execute in the
same run even when an individual case fails. Selenium, Pushgateway, Prometheus,
and Grafana are started together;
Grafana refreshes every 10 seconds as cumulative results are pushed after each
completed test.

## Multi-Project Support

Each application has its own `.testsuite.md` instruction file:

```
testsuites/
├── trackpilot.testsuite.md    ← TrackPilot testing instructions
├── ijp-mobile.testsuite.md    ← Detailed IJustPaid Android instructions
└── desktop-app.testsuite.md   ← Repository-grounded native desktop baseline
```

To add a new application:
1. Create `testsuites/<app>.testsuite.md` following the page-by-page format
2. Run: `make test-suite SUITE=testsuites/<app>.testsuite.md`

No core code changes needed.

## Execution Modes

### Web Tests (Selenium — default)

```bash
make demo-web                                     # Recommended demo command
make test                                         # Default: TrackPilot
make test-ai                                      # Same as above
make test-web                                     # Same as above
make test-suite SUITE=testsuites/xxx.testsuite.md  # Any project

# Explicit target type:
make test-suite SUITE=testsuites/xxx.testsuite.md TARGET_TYPE=web
```

Open the live dashboard at `http://localhost:3000` (`admin` / `admin`) and the
browser viewer at `http://localhost:7900` (`secret`). Use `make demo-web` only
for a short presentation run; it intentionally limits coverage.

### Desktop Tests (Appium)

```bash
# Configure DESKTOP_* values in .env, then run:
make test-desktop

# Or override the suite:
make test-desktop SUITE=testsuites/myapp-desktop.testsuite.md

# Via CLI with environment variables:
TARGET_TYPE=desktop \
APPIUM_PLATFORM=macos \
APP_BUILD_PATH=/Applications/MyApp.app \
BUNDLE_ID=com.example.myapp \
  python -m automation.runner --suite testsuites/myapp-desktop.testsuite.md \
    --target-type desktop --appium-platform macos --app-build-path /Applications/MyApp.app
```

### Android Tests (Appium)

```bash
# Via Makefile:
make demo-mobile                                  # Default: IJustPaid
make test-mobile                                  # Full repo + APK-grounded suite
make test-mobile-detailed                         # Explicit alias for full suite
make test-mobile SUITE=testsuites/myapp-mobile.testsuite.md

# Via CLI:
python -m automation.runner \
  --suite testsuites/myapp-mobile.testsuite.md \
  --target-type mobile \
  --appium-platform android \
  --app-build-path /path/to/app.apk \
  --device-name emulator-5554 \
  --app-package com.example.app \
  --app-activity com.example.app.MainActivity
```

### iOS Tests (Appium)

```bash
python -m automation.runner \
  --suite testsuites/myapp-ios.testsuite.md \
  --target-type mobile \
  --appium-platform ios \
  --device-name "iPhone 15" \
  --app-build-path /path/to/app.ipa \
  --bundle-id com.example.app
```

### Robot Framework Tests (Legacy)

```bash
make test-robot    # All Robot suites
```

## Target Configuration

All project configuration is set via `.env` or runtime environment variables. No project names, URLs, or paths are hardcoded.

### Common Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TARGET_TYPE` | No | `web` (default), `mobile`, or `desktop` |
| `PROJECT_NAME` | Recommended | Logical project name for metrics/artifacts |
| `APP_REPO_URL` | Optional | Source repository URL for AI inspection |
| `APP_REPO_REF` | Optional | Branch/tag to inspect |
| `GH_TOKEN` | For private GitHub repos | GitHub token; keep only in `.env`/CI secrets |
| `TEST_SUITE` | Recommended | Path to `.testsuite.md` file |
| `REQUIRE_REPO_CONTEXT` | No | Fail early when source inspection is unavailable |
| `REQUIRE_BUILD_CONTEXT` | No | Fail early when build inspection is unavailable |

### Web Target (`TARGET_TYPE=web`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BASE_URL` | **Yes** | — | Web application URL |
| `BROWSER` | No | `chrome` | Browser name (`chrome`, `firefox`) |
| `SELENIUM_REMOTE_URL` | No | `http://selenium:4444` | Remote Selenium endpoint |

*Note: Neither `APP_BUILD_PATH` nor Appium variables are required for web targets.*

### Mobile Target (`TARGET_TYPE=mobile`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APPIUM_PLATFORM` | **Yes** | — | `android` or `ios` |
| `APP_BUILD_PATH` | Required* | — | Path to `.apk` or `.ipa` build artifact |
| `APPIUM_SERVER_URL` | No | `http://127.0.0.1:4723` | Appium server URL |
| `DEVICE_NAME` | Recommended | — | Device or emulator name |
| `APP_PACKAGE` | Optional* | — | Android package (e.g. `com.example.app`) |
| `APP_ACTIVITY` | Optional | — | Android main activity |
| `BUNDLE_ID` | Optional* | — | iOS bundle identifier |

*\*At least one app identity is required: `APP_BUILD_PATH`, `APP_PACKAGE`, or `BUNDLE_ID`.*
*\*`BASE_URL` is NOT required for mobile targets.*

### Desktop Target (`TARGET_TYPE=desktop`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APPIUM_PLATFORM` | **Yes** | — | `macos` or `windows` |
| `APP_BUILD_PATH` | Required* | — | Path to `.app` or `.exe` build artifact |
| `APPIUM_SERVER_URL` | No | `http://127.0.0.1:4723` | Appium server URL |
| `BUNDLE_ID` | Optional* | — | macOS bundle identifier |

*\*At least one app identity is required: `APP_BUILD_PATH` or `BUNDLE_ID`.*
*\*`BASE_URL` is NOT required for desktop targets.*

*(Note: `APP_PATH` is also supported as a backward-compatible alias for `APP_BUILD_PATH`)*

### Preferred target-specific variables

Use `MOBILE_*` and `DESKTOP_*` variables when both targets share one `.env`.
They are resolved automatically, while explicit generic command-line values
still take precedence.

```dotenv
MOBILE_APP_REPO_URL=https://github.com/your-org/your-mobile-app.git
MOBILE_APP_BUILD_PATH=/absolute/path/to/app-release.apk
MOBILE_APPIUM_PLATFORM=android
MOBILE_APP_PACKAGE=com.example.app
MOBILE_APP_ACTIVITY=com.example.app.MainActivity

DESKTOP_TEST_SUITE=testsuites/myapp-desktop.testsuite.md
DESKTOP_APP_REPO_URL=/absolute/path/to/local/desktop-repo
DESKTOP_APP_BUILD_PATH=/Applications/MyApp.app
DESKTOP_APPIUM_PLATFORM=macos
DESKTOP_BUNDLE_ID=com.example.myapp
```

For a private GitHub repository, an existing `gh auth login` session is used
automatically, or set `GH_TOKEN` (or `GITHUB_TOKEN`) in the uncommitted `.env`.
A local repository path is also accepted and avoids remote authentication.
Each run writes the inspected repository/build facts to
`inspection-context.json` beside the test plan and results.

Web runs also write `coverage-summary.json`, showing parsed pages, categories,
Markdown instructions, generated tests, and the verified coverage percentage.

## Prerequisites

### Web Testing
- Docker (for Selenium container)
- No additional setup needed

### Desktop Testing (Appium)
- [Appium Server](https://appium.io/) 2.x installed: `npm install -g appium`
- **macOS**: `appium driver install mac2`
- **Windows**: `appium driver install windows`
- Host permissions for accessibility/automation

### Android Testing (Appium)
- Appium Server 2.x installed
- `appium driver install uiautomator2`
- Android SDK / Android Studio with emulator or connected device
- `ANDROID_HOME` environment variable set

### iOS Testing (Appium)
- macOS only
- Appium Server 2.x installed
- `appium driver install xcuitest`
- Xcode with simulators or connected device
- Valid Apple developer certificate for real devices

## Project Structure

```
├── testsuites/                      ← Testing instruction files (.testsuite.md)
│   └── trackpilot.testsuite.md
├── automation/                      ← Core framework
│   ├── ai/                          ← AI reasoning layer
│   │   ├── suite_parser.py          ←   Parse testsuite.md → structured context
│   │   ├── repo_reader.py           ←   Inspect repository UI/routes/test IDs
│   │   ├── build_inspector.py       ←   Inspect APK/IPA/.app/.exe metadata
│   │   └── test_generator.py        ←   Generate test cases + actions
│   ├── actions/                     ← Structured action layer
│   │   ├── schema.py                ←   Action/TestCase/TestResult schemas
│   │   ├── executor.py              ←   Selenium action executor (web)
│   │   └── appium_executor.py       ←   Appium action executor (desktop/mobile)
│   ├── validation/                  ← Validation + evidence
│   │   └── evidence.py              ←   Failure evidence collection
│   ├── listeners/                   ← Robot Framework listeners
│   │   └── prometheus_listener.py   ←   Prometheus metrics (Robot mode)
│   ├── metrics/                     ← Metrics infrastructure
│   │   ├── metrics.py               ←   Prometheus metric definitions
│   │   └── publisher.py             ←   Pushgateway publisher
│   ├── utilities/                   ← Shared utilities
│   │   └── test_ids.py              ←   Test ID extraction
│   └── runner.py                    ← Main AI test orchestrator
├── tests/                           ← Validation tests
│   ├── robot/                       ←   Robot Framework tests (legacy)
│   └── test_appium_integration.py   ←   Appium integration validation
├── observability/                   ← Grafana + Prometheus config
├── scripts/                         ← Shell scripts
│   ├── up.sh                        ←   Start infrastructure
│   ├── down.sh                      ←   Stop infrastructure
│   ├── health.sh                    ←   Health check
│   ├── clean.sh                     ←   Cleanup
│   ├── run-suite.sh                 ←   Run AI tests for a suite
│   ├── mobile-preflight.sh          ←   Android emulator/Appium readiness
│   └── desktop-preflight.sh         ←   Desktop Appium readiness
├── artifacts/                       ← Test execution output
│   ├── runs/<run-id>/               ←   AI runner output
│   │   ├── test-plan.json           ←     Generated test plan
│   │   ├── inspection-context.json  ←     Repository/build evidence
│   │   ├── coverage-summary.json    ←     Markdown instruction coverage
│   │   ├── test-results.json        ←     Execution results
│   │   ├── failures.json            ←     Failure details
│   │   ├── summary.json             ←     Run summary
│   │   ├── execution.log            ←     Execution log
│   │   └── screenshots/             ←     Failure screenshots
│   └── robot/                       ←   Robot Framework output
├── docker-compose.yml               ← Service orchestration
├── Dockerfile.testrunner            ← Test runner container
└── docs/ARCHITECTURE.md             ← Phase 0 architecture
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Selenium | 4444 (VNC: 7900) | Browser automation |
| Pushgateway | 9091 | Metrics buffer |
| Prometheus | 9090 | Metrics storage |
| Grafana | 3000 | Dashboard (admin/admin) |

## Test Results

After execution, results are in `artifacts/runs/<run-id>/`:

- **test-plan.json** — Generated test cases with action steps
- **inspection-context.json** — Repository and exact build metadata used by AI
- **test-results.json** — Full execution results per test
- **failures.json** — Failed tests with evidence
- **summary.json** — Aggregate statistics
- **screenshots/** — Failure screenshots
- **Grafana** — Live dashboard at http://localhost:3000

## Environment Variables

See [`.env.example`](.env.example) for all configuration options.

Key variables:
- `TARGET_TYPE` — Automation target: `web` (default), `desktop`, or `mobile`
- `PROJECT_NAME` — Logical project name for metrics/artifacts
- `APP_REPO_URL` / `MOBILE_APP_REPO_URL` / `DESKTOP_APP_REPO_URL` — repository URL or local path
- `BASE_URL` — Application URL (required for web targets)
- `APP_BUILD_PATH` — Generic application build path (`.apk`, `.ipa`, `.app`, `.exe`)
- `APPIUM_PLATFORM` — Appium platform: `android`, `ios`, `macos`, `windows`
- `APPIUM_SERVER_URL` — Appium server (default: `http://127.0.0.1:4723`)
- `TEST_SUITE` — Default suite file path
- `GEMINI_API_KEY` — Google Gemini API key for AI-driven test generation
- `VALID_USER_EMAIL` / `VALID_USER_PASSWORD` — Test credentials
