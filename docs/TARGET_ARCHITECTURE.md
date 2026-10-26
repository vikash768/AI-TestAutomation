# Target Architecture (proposal, 2026-10-14)

Companion to `CURRENT_STATE.md`. This is the design for the rebuild: give the platform a codebase URL and a live URL, and it discovers every interactive component, works out from the code what each one is supposed to do, checks that against the live application across functional, state, lifecycle, security and UI/UX dimensions, and reports findings in Grafana. It never fixes anything.

## 1. Inputs and outputs

| Input | Why it is needed |
|---|---|
| Codebase URL (git, public or with a token) or local path | Source of truth for intent: routes, components, handlers, validation rules, timers, guards |
| Live URL | The system under test |
| Test account credentials | To reach authenticated pages; a disposable account, never a real admin on production data |
| Optional scope | Routes to skip, actions to never perform (delete, pay, send), max run time |

| Output | Where |
|---|---|
| Component inventory with coverage status | Postgres → Grafana "Components" dashboard |
| Findings with severity, category, standard reference, evidence, confidence | Postgres → Grafana "Findings" dashboard |
| Run-level metrics and trends | Prometheus → Grafana "Runs" dashboard |
| Downloadable report (HTML/JSON) | `artifacts/runs/<run-id>/report.html` |

## 2. What changes from today

| Today | Target |
|---|---|
| Hand-written markdown suite is the only input | Code + live app are the inputs; no per-project suite file required |
| Generator never opens a page | Explorer crawls the live app first; planner sees real DOM and real code |
| 513/514 cases have no assertion | Every case carries an oracle derived from code or an explicit standard |
| Selenium remote Chrome, one browser per case | Playwright: persistent contexts, network interception, viewport emulation, tracing, console capture |
| Metrics-only Pushgateway gauges | Postgres for findings and evidence, Prometheus for run metrics, Grafana reads both |
| CLI + Make | Web UI to submit a run, watch progress, open the report |

## 3. Pipeline

Seven stages run by a worker. Each writes its output to the run store so a run can resume.

1. **Ingest code.** Clone, detect framework (React/Next/Vue/Angular/Django/etc.), parse with tree-sitter. Extract routes, components, interactive elements (buttons, links, inputs, forms, menus, modals), event handlers, API calls, validation rules (HTML attributes, zod/yup/Formik, server validators), auth guards, timers/idle logout/session expiry, persistence (localStorage, cookies, query state). An LLM summarises each component into an **intent spec**: "what this is supposed to do, when state should persist, what must never happen". Output: `intent_specs`.
2. **Explore live.** Playwright logs in and crawls breadth-first from the live URL. Per page: accessibility tree, interactive elements with stable selectors, screenshots at 5 viewports (360, 768, 1024, 1440, 1920), network log, console log. Safe-click policy: clicks that match destructive verbs or scope exclusions are recorded, not performed. Output: `observed_inventory`.
3. **Reconcile.** Match intent specs to observed elements (route + role + label + handler). Unmatched code elements → "dead or unreachable UI" findings. Unmatched live elements → "undocumented UI" findings. Output: `component_matrix` with coverage %.
4. **Plan tests.** For each component, generate cases using named techniques with the oracle attached: equivalence partitioning and boundary values for inputs (ISTQB), state-transition for navigation/persistence/back/refresh, decision tables for forms, lifecycle cases for timers and session rules, OWASP WSTG cases for auth/session/input handling, WCAG 2.2 AA checks, responsive layout checks per viewport, Nielsen heuristics for UX review. Output: `test_plan` (JSON, inspectable before execution).
5. **Execute.** Playwright executor with the existing action vocabulary extended (viewport, clock control, route abort/mock, storage inspection). axe-core injected per page. OWASP ZAP baseline (passive) over the crawl proxy; targeted non-destructive active checks (reflected input, security headers, cookie flags, protected-route access without session, rate limiting on login, CSRF token presence). Full trace per case.
6. **Judge.** Deterministic assertions decide where they can. Anything else (UI/UX, "looks broken") goes to an LLM judge that sees only evidence (screenshots, DOM diff, intent spec) and must cite it. Failing cases re-run twice; a finding is reported only if it reproduces and its confidence ≥ threshold, otherwise it lands in "needs review".
7. **Report.** Write components, tests, findings, evidence to Postgres; push run metrics to Prometheus; render `report.html`; mark run complete.

## 4. Test dimensions and the standards they follow

| Dimension | What is checked | Standard / technique |
|---|---|---|
| Functional | Each component does what the code intends; validation rules enforced; error handling | ISTQB test design (EP, BVA, decision table, state transition) |
| State & persistence | Refresh, back/forward, deep link, logout, storage contents, form drafts | State-transition testing |
| Lifecycle | Idle timeout, session expiry, auto-stop criteria from code (timers, counters) | Clock control + code-derived expectation |
| Stability | Actions that crash, blank, or close the app; console errors; failed requests | Error-guessing, negative testing |
| Security | Auth, session, access control, input handling, headers, cookies, info leakage | OWASP WSTG v4.2, ASVS L1/L2; CVSS for severity |
| UI / responsive | Overflow, overlap, clipped text, unreachable controls, layout shift at each viewport | Responsive checks + Core Web Vitals |
| Accessibility | Contrast, labels, focus order, keyboard reachability | WCAG 2.2 AA via axe-core |
| UX heuristics | Feedback, consistency, error prevention, recognition over recall | Nielsen's 10 heuristics (LLM-judged, evidence-cited) |
| Consistency | Same data shown on different pages agrees | Cross-page oracle |

## 5. Accuracy: what ">90%" means here

- **Precision** of reported findings ≥ 0.90: at least 9 of 10 reported bugs are confirmed real by a human reviewer.
- **Coverage** ≥ 0.90: at least 90% of interactive components found in the code are exercised on the live app.
- Mechanisms: oracle comes from code, not assumption; deterministic checks before LLM judgment; the judge must cite evidence; each failure re-run twice; confidence threshold calibrated on the first two reviewed runs; everything below threshold goes to "needs review", never silently dropped.
- Measured per run and shown on the Runs dashboard. The number is earned, not promised: the first runs on TrackPilot establish the baseline.

## 6. Data model and Grafana

Tables: `runs`, `components`, `tests`, `findings`, `evidence`, `review_labels`.

Finding fields: run_id, component_id, page, category, severity, title, expected, observed, standard_ref (e.g. WSTG-ATHN-03, WCAG 1.4.3), viewport, confidence, reproduced_n_of_m, status (reported / needs_review / confirmed / rejected), evidence links.

Dashboards (Postgres datasource for tables, Prometheus for trends):
- **Runs**: coverage %, findings by severity, precision from reviewed labels, duration, trend across runs.
- **Findings**: filterable table (category, page, component, severity, viewport, status) with evidence links.
- **Components**: matrix of component × dimension with status.
- **Security**: WSTG category breakdown, headers/cookies summary.
- **UI/UX**: issues per viewport, accessibility rule hits.

## 7. Stack

Python 3.12 · FastAPI (API + UI) · Playwright (Python) · tree-sitter · LLM provider abstraction (Gemini via google-genai with a current model, or Claude) · axe-core · OWASP ZAP (docker, baseline) · Postgres · Prometheus (kept) · Grafana (kept; Postgres datasource added) · one worker container.

Removed: Selenium container, Robot Framework, SeleniumLibrary, markdown-suite parser, Pushgateway (replaced by a `/metrics` endpoint on the API, scraped directly).

## 8. Roadmap

| Phase | Delivers | Exit criterion |
|---|---|---|
| 1 Foundation | Playwright executor, code ingestor, live explorer, reconcile, Postgres + Grafana dashboards, minimal UI | Every interactive component in TrackPilot's code is listed with observed status; smoke click on each; report in Grafana |
| 2 Functional depth | Planner with code-derived oracles, persistence/lifecycle/stability cases, re-run and confidence gate | Precision ≥ 0.90 on a reviewed sample |
| 3 Quality dimensions | Responsive + accessibility + security + UX heuristics | Each dimension produces evidence-backed findings on TrackPilot |
| 4 Calibration & multi-project | Threshold tuning from review labels, second application, report polish | Two applications tested end to end from the UI alone |

## 9. Guardrails

- Report only. The platform never edits the application under test.
- Non-destructive by default: delete/pay/send/disable actions are detected and skipped unless the scope explicitly allows them on disposable data.
- Security checks are passive plus a fixed set of non-destructive active probes, run only against URLs the operator is authorised to test. No denial-of-service style load.
- Credentials never enter an LLM prompt; the planner sees "<credential>" placeholders substituted at execution time.

## 10. Decisions (settled 2026-10-14)

1. Playwright replaces Selenium. Done.
2. LLM provider: Gemini, `GEMINI_MODEL` with `GEMINI_FALLBACK_MODEL` for quota or outage responses.
3. Findings store: Postgres, read by Grafana through its native datasource. Done.
4. First target: `tek-pods/trackpilot_FE` (React 18, react-router 6, Vite) against https://trackpilot.pods365.com.
5. The live target is the team's own pre-production deployment; security checks stay non-destructive by design.

Phase 1 is implemented; see `ARCHITECTURE.md` for the as-built description.
