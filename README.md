# AI Testing Automation

AI-driven test automation platform that reads project-specific testing instructions and dynamically generates, executes, and reports test results.

## Architecture

```
testsuites/trackpilot.testsuite.md   ← Testing instructions (source of truth)
        ↓
AI Suite Parser                       ← Parse markdown into structured context
        ↓
AI Test Generator                     ← Generate concrete test cases + actions
        ↓
Structured Actions                    ← NAVIGATE / CLICK / TYPE / VERIFY / etc.
        ↓
Selenium Executor                     ← Deterministic browser commands
        ↓
Chrome Browser                        ← Remote via Selenium container
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

# 4. Run AI-driven tests (default: TrackPilot)
make test-ai

# 5. Run tests for a specific suite
make test-suite SUITE=testsuites/trackpilot.testsuite.md

# 6. View results in Grafana
open http://localhost:3000

# 7. Reset environment and wipe test results to run fresh tests
make clean
```

## Multi-Project Support

Each application has its own `.testsuite.md` instruction file:

```
testsuites/
├── trackpilot.testsuite.md    ← TrackPilot testing instructions
├── ijp.testsuite.md           ← (future) IJP testing instructions
└── ecommerce.testsuite.md     ← (future) E-commerce testing instructions
```

To add a new application:
1. Create `testsuites/<app>.testsuite.md` following the page-by-page format
2. Run: `make test-suite SUITE=testsuites/<app>.testsuite.md`

No core code changes needed.

## Execution Modes

### AI-Driven Tests (Primary)
```bash
make test                                       # Default: TrackPilot
make test-ai                                    # Same as above
make test-suite SUITE=testsuites/xxx.testsuite.md  # Any project
./scripts/run-suite.sh testsuites/trackpilot.testsuite.md
```

### Robot Framework Tests (Legacy)
```bash
make test-robot    # All Robot suites
```

## Project Structure

```
├── testsuites/                      ← Testing instruction files (.testsuite.md)
│   └── trackpilot.testsuite.md
├── automation/                      ← Core framework
│   ├── ai/                          ← AI reasoning layer
│   │   ├── suite_parser.py          ←   Parse testsuite.md → structured context
│   │   └── test_generator.py        ←   Generate test cases + actions
│   ├── actions/                     ← Structured action layer
│   │   ├── schema.py                ←   Action/TestCase/TestResult schemas
│   │   └── executor.py              ←   Selenium action executor
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
├── tests/robot/                     ← Robot Framework tests (legacy)
│   ├── suites/trackpilot/login.robot
│   ├── resources/                   ← Reusable keywords
│   └── variables/                   ← Environment variables
├── observability/                   ← Grafana + Prometheus config
├── scripts/                         ← Shell scripts
│   ├── up.sh                        ←   Start infrastructure
│   ├── down.sh                      ←   Stop infrastructure
│   ├── health.sh                    ←   Health check
│   ├── clean.sh                     ←   Cleanup
│   └── run-suite.sh                 ←   Run AI tests for a suite
├── artifacts/                       ← Test execution output
│   ├── runs/<run-id>/               ←   AI runner output
│   │   ├── test-plan.json           ←     Generated test plan
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
- **test-results.json** — Full execution results per test
- **failures.json** — Failed tests with evidence
- **summary.json** — Aggregate statistics
- **screenshots/** — Failure screenshots
- **Grafana** — Live dashboard at http://localhost:3000

## Environment Variables

See [`.env.example`](.env.example) for all configuration options.

Key variables:
- `BASE_URL` — Application URL
- `VALID_USER_EMAIL` / `VALID_USER_PASSWORD` — Test credentials
- `TEST_SUITE` — Default suite file path
