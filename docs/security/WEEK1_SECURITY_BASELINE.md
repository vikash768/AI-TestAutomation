# Week 1 Security Baseline

Audit date: 2026-10-17

Branch: `feature/week1-security-hardening`

## Scope

This baseline covers the Week 1 credential-handling and repository-hygiene controls for:

- Gemini test planning and generated test structures.
- Selenium web execution.
- Appium Android, iOS, macOS, and Windows execution.
- Legacy Robot Framework execution.
- Test-plan, result, failure, cache, inspection, coverage, log, and metrics output.
- Git tracking rules, recognizable secret patterns, and risky filenames in reachable history.
- Docker build context and required runtime password configuration.
- The local Python test environment and security regression suite.

The audit did not open `.env`, inspect existing AI-cache or historical artifact contents, execute a production login, contact Gemini, or start a live Selenium/Appium application flow.

## Completed Security Controls

### Task 1 — Credential lifecycle and serialization

- Added extensible symbolic placeholders such as `{{VALID_USER_EMAIL}}` and `{{VALID_USER_PASSWORD}}`.
- Added the shared `SecretResolver` with controlled missing-secret errors.
- Prevented application credentials from being supplied to Gemini or required by the planner.
- Kept generated `Action` and `TestCase` values symbolic through planning and validation.
- Added execution-boundary resolution for Selenium and Appium using temporary action copies.
- Added centralized recursive sanitization for sensitive keys, known environment secrets, credential-bearing URLs, sensitive query parameters, and authorization-like values.
- Protected `Action`, `TestCase`, `TestResult`, cache, failure, and log serialization.
- Moved hardened caches to the `safe_v2` namespace so pre-hardening cache names are not loaded.

### Task 2 — Repository, logging, metrics, and Docker hygiene

- Hardened `.gitignore` for environment files, generated artifacts, caches, private keys, credential JSON, IDE files, and application build packages.
- Added `.dockerignore` so local configuration, credentials, artifacts, Git metadata, and build packages do not enter Docker build contexts.
- Added `scripts/security-check.sh` for tracked-path, recognizable token/key pattern, ignore-rule, and whitespace checks.
- Sanitized repository URLs, errors, runner endpoint logs, metric labels, NoOp payloads, and screenshot filenames.
- Removed committed weak password defaults for Grafana and Selenium VNC; both now require local environment values.

### Task 3 — Legacy Robot Framework

- Replaced resolved credential variables with symbolic Robot variables.
- Added `automation.robot.secure_keywords`, backed by the shared `SecretResolver`.
- Resolved credentials inside the secure Python keyword immediately before Selenium input.
- Used SeleniumLibrary's protected password-input method without returning the resolved value to Robot.
- Tagged secret keywords and configured Robot with `--removekeywords tag:secret`.
- Fixed the Robot Prometheus listener to provide the complete publisher signature.
- Added one stable Robot `run_id` for each listener execution.
- Sanitized Robot-derived metric values before publication.

### Final audit controls

- Confirmed no dangerous tracked paths in the reviewed categories.
- Confirmed recognizable source-secret scans pass.
- Confirmed no obvious secret-bearing filenames in reachable Git history.
- Reviewed all principal credential, persistence, logging, metrics, Robot, and Docker paths.
- Validated Docker Compose against an explicit empty environment file with ephemeral dummy required values.

## Credential Lifecycle

```text
local process environment
        ↓
symbolic placeholder
        ↓
AI plan / TestCase / Action
        ↓
execution-time SecretResolver
        ↓
Selenium / Appium / secure Robot keyword
        ↓
application under test
```

Gemini, AI planning, generated plans, and AI cache files should receive only symbolic placeholders or redacted values. They should never receive resolved application credentials.

Selenium and Appium create temporary execution-action copies, resolve placeholders immediately before handlers run, and leave the persistent source actions symbolic. Robot keeps credential variables symbolic and resolves them only inside the protected Python keyword immediately before calling SeleniumLibrary.

Provider authentication such as the Gemini API key remains process configuration used to initialize the provider client; it is not interpolated into prompts or artifacts.

## Protected Outputs

| Output | Protection |
|---|---|
| AI prompts | Symbolic application credentials plus final prompt sanitization |
| AI cache | Hardened namespace, sanitized `to_cache_dict()` values, recursive sanitization before write |
| Test plans | Sanitized `TestCase.to_dict()` and nested `Action.to_dict()` |
| Test results and failures | Sanitized expected/actual values, errors, console/API errors, URLs, and nested data |
| Summary and execution logs | Recursive sanitization before persistence |
| Repository/build inspection | Sanitized structured inspection output and repository URLs/errors |
| Coverage output | Recursive sanitization before persistence |
| Prometheus metrics | Sanitized labels, grouping values, endpoints, and NoOp payloads |
| Robot reports | Resolved values stay outside Robot arguments; secret keywords are removable and output remains under ignored artifacts |
| Screenshots | Secret-safe filenames and ignored output paths; image pixels are not automatically redacted |
| Docker build context | Environment files, keys, artifacts, caches, Git metadata, and app packages excluded |

Normal non-sensitive test data remains available for debugging. Redaction is driven by sensitive key/context detection and known environment-secret replacement rather than blanket removal of all values.

## Validation Results

Final audit results:

- `python3 -m pytest -q`: **81 passed, 23 warnings**.
- `python3 -m pytest -q tests/test_secret_handling.py`: **15 passed, 10 warnings**.
- `python3 -m pytest -q tests/test_robot_security.py`: **5 passed**.
- Robot dry run: **25 passed, 0 failed, no Robot errors**.
- `python3 -m compileall -q automation`: **passed**.
- `git diff --check`: **passed**.
- `./scripts/security-check.sh`: **passed**.
- `bash -n scripts/*.sh`: **passed**.
- Docker Compose validation with `/dev/null` instead of local `.env`: **passed**.
- Dangerous tracked-path audit: **no findings**.
- Reachable-history risky-filename audit: **no findings**.
- `gitleaks`: **not installed; not run**.

Local environment baseline:

- Python **3.9.6**, Apple Command Line Tools system Python.
- pytest **8.4.2** from user-site packages.
- No active virtual environment or Conda environment.
- Observed warnings cover Python 3.9 end-of-life, LibreSSL compatibility, an upstream Pydantic deprecation, and pytest collection warnings for domain classes whose names begin with `Test`.
- A future environment task should move development to Python 3.12 in a repository-local `.venv`.

## Remaining Risks

| Risk | Severity | Current treatment / next action |
|---|---|---|
| Selenium/Appium/WebDriver wire or infrastructure logging can observe values after execution-time resolution | MEDIUM | Keep driver/server debug logging restricted and review infrastructure log retention |
| Screenshots can visually capture sensitive information displayed by the application | MEDIUM | Add screenshot redaction or capture policy where sensitive screens are tested |
| Historical local artifacts created before Week 1 may contain credentials | MEDIUM | Do not publish them; review and remove them through an approved, separate cleanup process |
| Git history has not received a content-level scan because `gitleaks` is unavailable | MEDIUM | Run a redacted history scan before public or high-assurance release |
| Robot remains a legacy secondary execution path that can drift from the primary executors | LOW | Keep focused tests while supported; deprecate after equivalent coverage migrates |
| Future code could bypass `SecretResolver` or the serializers | MEDIUM | Enforce security regression tests and repository checks in CI |
| CI does not currently enforce the security script and focused tests | MEDIUM | Add a required security workflow in Week 2 or the next CI task |
| Branch protection is not established by this local audit | MEDIUM | Configure required reviews and required passing security checks in repository settings |
| Python 3.9 is end-of-life and the local TLS stack uses LibreSSL | MEDIUM | Move to Python 3.12 plus a local `.venv`; refresh dependencies in a separate environment task |

No HIGH residual risks were identified by this local audit.

## Merge Considerations

This branch is four commits ahead of `main`. It contains:

1. The earlier cross-platform local development snapshot.
2. Week 1 Task 1 credential and serialization hardening.
3. Week 1 Task 2 repository and Docker hygiene hardening.
4. Week 1 Task 3 Robot Framework hardening.

A pull request from this branch to `main` is **not a security-only PR**. It also includes substantial Selenium/Appium mobile and desktop execution, repository/build inspection, suite, test, and reporting changes from the earlier cross-platform snapshot. Review and release planning must account for that broader scope.

## Week 1 Exit Decision

**PASS — ready to begin Week 2**

No application-credential blocker, dangerous tracked path, weak committed Docker password default, or failing security regression test remains in the reviewed scope. The remaining risks are documented follow-up items rather than Week 1 exit blockers.
