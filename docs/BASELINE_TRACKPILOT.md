# Baseline: TrackPilot Admin, 2026-10-14

First clean run of the rebuilt platform (run `7c979cf5dfd3`, commit `d7d9357790` of `tek-pods/trackpilot_FE`, live https://trackpilot.pods365.com). Use this to judge later runs and the refactor's accuracy work.

## Numbers

| Measure | Value |
|---|---|
| Pages explored | 19 (all code routes), each at 5 viewports |
| Interactive components declared in code | 287 |
| Always rendered at rest (not behind a drawer, menu, tab, condition or list) | 61 |
| Always-rendered components found on the live app | 61 (100%) |
| All components found at rest | 117 (41%; the rest render only after an interaction) |
| Live controls with no code declaration | 0 |
| Tests | 248: 242 pass, 6 fail |
| Smoke clicks | 79 of 79 pass; 10 skipped by policy (mutating / destructive / download) |
| Route guards (unauthenticated access to protected routes) | 16 of 16 redirect to /login |
| Logout ends the session and re-guards protected routes | pass |
| Findings | 6 reported, 0 held for review |
| Duration | 6.3 minutes (LLM summaries cached) |

## Findings (all deterministic observations with screenshots)

| Severity | Where | What | Standard |
|---|---|---|---|
| medium | `/` and `/download` at 360 px and 768 px | Page is wider than the viewport: a 460 px decorative block extends past the edge, so the landing page scrolls sideways on phones and tablets | WCAG 2.2 1.4.10 Reflow |
| low | `/reports` at 360 px and 768 px | The six "Open Report" links are 23 px tall, one pixel under the minimum target size | WCAG 2.2 2.5.8 Target Size |

Everything else observed was healthy: every page returned HTTP 200 with zero console errors and zero failed requests, no click produced an error, blank page, lost session or wrong destination.

## Earlier runs and what they taught

Four runs were needed to get here. Each earlier run exposed a defect in the platform, not in the app, and each became a test:

1. The saved browser session was stale by the time the smoke stage started; the stage now signs in itself.
2. A drawer without a dialog role stayed open and intercepted later clicks, producing eight confident false positives; every click and every retry now starts from a fresh page load, and a page-state check counts covering overlays.
3. Six "Open Report" links shared one intent record because identical clones had identical keys.
4. Download links were judged as navigation; they are now classified as downloads and skipped.
5. The eye icon inside the password field and a native checkbox were flagged by the overlap and target-size rules; both now follow the standards' exemptions.

## What is still conditional

170 code components are not rendered at rest: drawer forms (add user, add project, edit role), confirm dialogs, dropdown items, date-picker internals, tab panels, owner-only sections, early-return states. Phase 2 opens those paths with the code's intent (open the drawer, then test its form) and adds input-level tests from the validation attributes already extracted.

---

# Phase 2 baseline, 2026-10-14

Run `e7d652888739`, same commit and target, all seven Phase 2 modules, write guard on.

## Numbers

| Measure | Phase 1 | Phase 2 |
|---|---|---|
| Code components found on the live app | 117 of 287 (41%) | 240 of 287 (84%), by opening menus, drawers, tabs and pickers |
| Found components exercised by a test | 80 | 86% (135 of them as the same control of a shared component tested on another page) |
| Tests | 248 | 488 |
| Writes that reached the server | n/a | only sign-ins, token refreshes and the one real logout test |
| Duration | 6 min | 24 min |

| Module | Tests | Findings | Notes |
|---|---|---|---|
| reveal | 110 | 6 | 29 openers, two levels deep; dialog checks on every new panel |
| destructive | 6 | 1 | 5 confirmations opened and cancelled; nothing confirmed |
| forms | 27 | 3 | 6 forms: profile, change password, add user, edit role, add project, add task |
| auth | 7 | 0 | masking, toggle, empty submit, unknown account, enumeration, Enter key |
| state | 24 | 2 | reload on 16 routes, new tab, back/forward, logout offline, back after logout, unsaved edits |
| lifecycle | 5 | 0 | Live Activity every 15 s, notifications every 60 s, Auto-refresh off stops it, leaving stops it |
| faults | 59 | 0 | 502, offline and slow responses on every page's own data; every Retry recovered |

## New findings (each checked against the source)

| Severity | Status | Finding | Why it is real |
|---|---|---|---|
| medium | reported | Logout keeps the session when the logout request fails | `api/auth.js` `logout()` awaits the request before `clearSession()`, so a failed request skips it; `AppShell.jsx`'s comment says the opposite. Reproduced 2/2. |
| medium | reported | Drawer (Add User, Add Project, Edit Project, Edit Role, Add Task) is not exposed as a dialog | `components/ui/Drawer.jsx` renders a plain div: no `role="dialog"`, no `aria-modal`. WCAG 4.1.2. |
| low | reported | Focus stays behind the drawer | No focus management in `Drawer.jsx`. WCAG 2.4.3. |
| low | reported | Drawer does not close with Escape | No key handler in `Drawer.jsx` (the menu and date picker do have one). WAI-ARIA dialog pattern. |
| medium | reported | Screenshot viewer is not exposed as a dialog | `pages/Screenshots.jsx` overlay is a plain fixed div (it does close on Escape). |
| low | reported | Focus stays behind the screenshot viewer | No focus moved into the overlay. |
| low | reported | Focus stays behind the date picker | `DateRangePicker.jsx` declares `role="dialog"` but leaves focus on the trigger. |
| low | reported | Full Name / Project Name accept spaces only (Profile, Add User, Add Project) | `required` passes whitespace and the handlers send it unchanged; the Add Task form trims, the others do not. |
| medium | needs review | Email settings "Remove" deletes without a confirmation | `EmailSettingsTab.jsx` `remove()` calls `DELETE /email-settings` directly; every other destructive action uses `openConfirm`. Not clicked by the platform. |
| low | needs review | Unsaved Profile changes are discarded without warning | Heuristic (Nielsen #5). |

## Platform defects found on the way and fixed

1. Unanchored selectors: 1,425 of 1,636 stored selectors could match a different element; "Edit Role" resolved to the sidebar's Log out. Selectors are now always anchored, and every click first checks the element's name. Regression test added.
2. Shared components were tested once per page (a date picker on eight pages, every calendar day): 19 minutes for reveal. Now tested once and marked "equivalent" elsewhere; reveal takes 7.5 minutes.
3. A path pattern dropped a trailing `${memberId}` and called a correct endpoint "not found in code".
4. The configured `GITHUB_TOKEN` cannot read the private repository; local runs only worked through the Mac's keychain. The clone error now says so.
