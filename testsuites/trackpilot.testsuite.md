# TrackPilot Admin — AI Testing Instruction Suite

## Purpose

This file is the **project-specific testing instruction source** for the TrackPilot Admin application.

It is **not** a hardcoded Selenium script and it is **not** a static list of only prewritten test cases.

The AI test planner must read this file, inspect the actual TrackPilot UI, generate all applicable concrete test cases, convert them into deterministic browser actions, execute them through Selenium/Robot Framework, validate expected vs actual behavior, and record evidence/results.

---

# 1. Application Context

**Application:** TrackPilot Admin  
**Base URL:** supplied through environment/configuration  
**Authentication:** Admin credentials supplied through environment/configuration  
**Do not hardcode credentials in this file or source code.**

Expected environment variables:

```text
TRACKPILOT_BASE_URL
TRACKPILOT_ADMIN_EMAIL
TRACKPILOT_ADMIN_PASSWORD
```

Known admin routes from the supplied UI:

```text
/login
/dashboard
/users
/live
/history
/screenshots
/projects
/reports
/settings
/audit
/profile
```

The common sidebar also exposes **Log out**.

---

# 2. Required Test Execution Model

For every page:

```text
READ PAGE INSTRUCTIONS
        ↓
OPEN / NAVIGATE TO PAGE
        ↓
INSPECT ACTUAL UI
        ↓
IDENTIFY AVAILABLE CONTROLS / STATES
        ↓
GENERATE APPLICABLE TEST CASES
        ↓
CONVERT TO STRUCTURED ACTIONS
        ↓
EXECUTE IN SELENIUM
        ↓
VALIDATE EXPECTED VS ACTUAL
        ↓
PASS / FAIL / BLOCKED / SKIPPED
        ↓
CAPTURE EVIDENCE + LOGS
```

Do not invent functionality that is not present.

If a control, workflow, validation rule, or backend behavior cannot be confirmed from the live application, mark it:

```text
TO_BE_VERIFIED
```

---

# 3. Standard Test Case Metadata

Every AI-generated test case should contain:

```text
test_id
project
page
route
functionality
scenario
test_type
priority
preconditions
test_data
steps
expected_result
negative_expectation
automation_status
```

Test ID format:

```text
TC-TP-<PAGE>-<FUNCTION>-<NUMBER>
```

Examples:

```text
TC-TP-LOGIN-AUTH-001
TC-TP-DASH-CARD-001
TC-TP-USERS-SEARCH-001
TC-TP-LIVE-REFRESH-001
TC-TP-HISTORY-FILTER-001
TC-TP-SCREENSHOTS-DATE-001
TC-TP-PROJECTS-CRUD-001
TC-TP-REPORTS-OPEN-001
TC-TP-SETTINGS-GENERAL-001
TC-TP-AUDIT-SEARCH-001
TC-TP-PROFILE-EDIT-001
TC-TP-LOGOUT-SESSION-001
```

---

# 4. Standard Test Categories

Apply only where relevant:

```text
POSITIVE
NEGATIVE
VALIDATION
BOUNDARY
AUTHENTICATION
AUTHORIZATION
SESSION
NAVIGATION
CRUD
SEARCH
FILTER
SORT
PAGINATION
DATA_INTEGRITY
EMPTY_STATE
LOADING_STATE
ERROR_HANDLING
BROWSER_BEHAVIOUR
PERMISSIONS
WORKFLOW
SMOKE
REGRESSION
E2E
```

For each page, also consider:

- page load
- route correctness
- page heading
- visible controls
- primary actions
- keyboard interaction where practical
- refresh behavior
- browser Back / Forward behavior
- empty state
- loading state
- API/network failure behavior
- browser console errors
- user-facing error handling
- role/permission restrictions
- data consistency
- duplicate click / rapid click behavior where meaningful

---

# 5. Safety Rules

This environment may contain real organization data.

Do **not**:

- delete real users
- disable real users
- change a real user's password
- delete production projects
- permanently modify organization-wide settings
- perform irreversible destructive actions on business data

For mutation tests:

1. Use dedicated disposable test data.
2. Create clearly identifiable temporary records.
3. Clean them up after testing.
4. If safe test data is unavailable, mark the case `BLOCKED`.

---

# 6. Page 1 — Login

**Route:** `/login`

## Visible Functionality

- Email input
- Password input
- Password show/hide control
- Sign in button
- Admin portal heading
- TrackPilot branding

## 6.1 Authentication

The AI must generate tests for:

- valid admin email + valid password
- valid email + invalid password
- invalid/unknown email + password
- both credentials invalid
- retry with valid credentials after failed login
- successful redirect to authenticated area
- authenticated admin identity visible after login

Avoid excessive invalid attempts that could lock an account.

## 6.2 Email Input Validation

Test applicable behavior for:

- empty email
- whitespace-only input
- leading spaces
- trailing spaces
- valid email format
- missing `@`
- missing domain
- multiple `@`
- uppercase/lowercase handling
- special characters
- very long input

Do not assume validation rules; verify actual behavior.

## 6.3 Password Input

Test:

- empty password
- incorrect password
- password masked by default
- show password
- hide password again
- leading/trailing spaces
- special characters
- long password
- Enter-key submission if supported

## 6.4 Submission

Test:

- normal Sign in click
- submit with both fields empty
- submit with one field empty
- rapid/double click
- retry after error
- loading/disabled state during request if present

## 6.5 Error Handling

Verify:

- invalid login shows controlled user-facing feedback
- no raw stack trace
- no sensitive backend details exposed
- page remains usable after failure
- retry is possible
- API/network authentication failures are logged

## 6.6 Session / Security-Oriented Functional Checks

Verify:

- protected route before login
- refresh after successful login
- direct `/dashboard` before authentication
- opening login page while already authenticated
- logout invalidates session
- browser Back after logout does not restore protected data
- direct protected URL after logout redirects/rejects appropriately

---

# 7. Page 2 — Dashboard

**Route:** `/dashboard`

## Visible Functionality

Summary cards:

- Team Members
- Tracking Now
- paused count
- Hours (30 Days)
- Active Projects

Sections:

- Tracking right now
- View all
- employee name
- tracked duration today
- running/paused status
- Hours by employee
- Full report
- employee name
- 30-day duration

Shared header:

- notification icon
- administrator profile control

## 7.1 Page Load

Verify:

- route opens
- Dashboard heading displays
- sidebar Dashboard state is active
- summary cards render
- data sections render
- no blocking console/API errors

## 7.2 Summary Cards

For each card verify:

- card visible
- label correct
- value present
- expected data format
- value is not malformed
- refresh does not produce inconsistent UI

Do not hardcode current counts.

Where possible compare:

- Team Members count with Users data
- Tracking Now value with Live Activity data
- Active Projects count with Projects list
- Hours (30 Days) with relevant report/history data

## 7.3 Tracking Right Now

Verify:

- section loads
- employee rows render
- employee names display
- tracked duration format is valid
- status badge displays
- running status handling
- paused status handling
- zero/no-active-user state
- multiple-user state
- View all navigates to the correct related page
- data remains consistent after refresh

## 7.4 Hours By Employee

Verify:

- section loads
- employee rows render
- durations display in valid format
- long hour values display correctly
- Full report opens correct report flow
- empty state
- data refresh
- visible data is logically consistent with reporting/history where possible

## 7.5 Header Actions

Test:

- notification icon interaction
- profile control interaction
- displayed admin identity/role
- resulting popover/page if applicable

If no interaction exists, mark it accordingly instead of inventing behavior.

## 7.6 Dashboard Navigation

Verify sidebar links:

- Users
- Live Activity
- Time History
- Screenshots
- Projects & Tasks
- Reports
- Settings
- Audit Logs
- Profile

For each:

- click works
- correct route
- correct page heading
- active menu item updates

---

# 8. Page 3 — Users

**Route:** `/users`

## Visible Functionality

- Team Members section
- Add User
- Search users
- Status filter
- Users table
- Name
- Email
- Role
- Status
- Actions menu

## 8.1 Page Load

Verify:

- Users route opens
- Users heading
- Team Members section
- table headers
- rows render
- status badges render
- Add User visible for authorized admin

## 8.2 Search

Generate tests for:

- exact user name
- partial user name
- exact email
- partial email if supported
- case handling
- leading/trailing spaces
- no-result query
- special characters
- empty search
- clear search
- search after filter
- filter after search

Verify returned rows actually match the search behavior.

## 8.3 Status Filter

Test all discovered status options.

Verify:

- default `All Status`
- each available status
- filter result correctness
- no-result state
- filter + search
- clearing/reset behavior
- filter state after refresh if applicable

## 8.4 User Table

Verify:

- Name column
- Email column
- Role column
- Status column
- Actions column
- each visible row maps data correctly
- long names/emails do not break layout
- empty state if no users

## 8.5 Add User

Open Add User and dynamically inspect the actual form.

Generate tests for every discovered field and validation rule:

- valid create
- required fields
- email format
- duplicate email
- role selection
- default status if present
- cancel
- save
- invalid submission
- backend failure
- duplicate click

Use disposable test users only.

## 8.6 Actions Menu

Open Actions for a safe test user and discover available operations.

Potential actions may include view/edit/status/role/delete, but only test actions that actually exist.

For every discovered action:

- verify menu opens
- action label
- permissions
- confirmation behavior
- validation
- success feedback
- cancel behavior
- persistence after refresh

Do not perform destructive actions on real users.

## 8.7 Authorization

Where additional roles/test accounts are available:

- verify restricted actions are hidden/disabled
- direct URL access is controlled
- admin-only actions remain admin-only

If role accounts are unavailable, mark as `BLOCKED` or `TO_BE_VERIFIED`.

---

# 9. Page 4 — Live Activity

**Route:** `/live`

## Visible Functionality

Working right now:

- summary count (`tracking`, `paused`)
- Auto-refresh checkbox
- Refresh button
- Employee
- Project
- Task
- Started At
- Status
- Tracked Today
- Activity percentage
- Idle
- Keys/Min
- Clicks/Min
- Last Signal

Finished today:

- completed sessions section
- empty state when no completed sessions exist

## 9.1 Page Load

Verify:

- route and heading
- Working right now section
- Finished today section
- table headers
- active sidebar state

## 9.2 Live Activity Data

Verify each row:

- employee name
- project
- task
- started time
- running/paused status
- tracked duration
- activity percentage
- idle duration
- key rate
- click rate
- last signal

Validate data types/format and logical consistency.

Examples:

- activity percentage should be valid percentage data
- tracked duration should use a consistent duration format
- negative durations should never display
- status should use supported values

## 9.3 Refresh

Test:

- manual Refresh
- data reload occurs
- page stays usable
- no duplicate rows
- failure response handled cleanly

## 9.4 Auto-refresh

Test:

- enabled state
- disabled state
- toggling checkbox
- data update behavior while enabled
- no unexpected update while disabled, if that is intended behavior

Do not assume refresh interval unless observable/configured.

## 9.5 Finished Today

Verify:

- completed sessions render when available
- empty state when none exist
- displayed totals/rows are formatted correctly
- no stale data after refresh

---

# 10. Page 5 — Time History

**Route:** `/history`

## Visible Functionality

- employee filter
- project filter
- From date
- To date
- Refresh
- entries count
- page duration total
- results / empty state

## 10.1 Page Load

Verify:

- heading
- active sidebar
- default filters
- date controls
- Refresh
- result region

## 10.2 Employee Filter

Test:

- All employees
- individual employee
- available options
- result correctness
- no-result state
- employee + project combination

## 10.3 Project Filter

Test:

- All projects
- individual project
- available options
- result correctness
- no-result state
- project + employee combination

## 10.4 Date Range

Test:

- same-day range
- valid multi-day range
- past date
- current date
- start before end
- start equal end
- start after end
- invalid date entry if manual input is allowed
- date range with employee/project filters

Verify validation/error behavior rather than assuming rules.

## 10.5 Refresh / Results

Verify:

- filters apply on Refresh or immediately, according to actual behavior
- result count
- duration total
- empty-state message
- data does not duplicate
- selected filters remain correct after request

---

# 11. Page 6 — Screenshots

**Route:** `/screenshots`

## Visible Functionality

- team/employee selector
- From date
- To date
- Refresh
- capture count
- screenshot result area
- empty state

## 11.1 Page Load

Verify:

- page heading
- active sidebar
- default team selector
- date controls
- Refresh button
- capture count/result region

## 11.2 Team / Employee Filter

Test:

- Whole team
- each available employee
- no-result employee
- selector + date range combination

## 11.3 Date Range

Test:

- valid same-day range
- multi-day range
- past/current date
- start after end
- employee + date range
- empty result period

## 11.4 Screenshot Results

When screenshots exist:

- thumbnails/cards render
- employee association is correct
- timestamps if shown are valid
- opening a screenshot works
- close/navigation controls work
- broken/missing image handling
- loading behavior

Do not expose/download sensitive screenshots unless necessary for the test.

## 11.5 Refresh

Verify:

- data refresh
- no duplicate captures
- current filters preserved
- API/network failures handled cleanly

---

# 12. Page 7 — Projects & Tasks

**Route:** `/projects`

## Visible Functionality

Tabs:

- Projects
- Tasks
- Deleted

Projects tab:

- Add Project
- Name
- Description
- Assigned
- Assigned By
- Status
- Actions
- Edit
- Delete

## 12.1 Tab Navigation

Test:

- Projects tab
- Tasks tab
- Deleted tab
- active tab state
- browser refresh on each tab
- Back/Forward behavior where applicable

## 12.2 Projects Listing

Verify:

- list renders
- columns correct
- project names
- descriptions
- assigned users
- assigned by
- status
- actions

Check long/empty descriptions and multiple assignees where available.

## 12.3 Add Project

Open Add Project and inspect actual form.

Generate cases for:

- valid project
- required fields
- optional description
- assignment
- duplicate name behavior
- invalid data
- cancel
- save
- error response
- rapid/double submit

Use a disposable project.

## 12.4 Edit Project

Using only disposable/safe project data:

- open Edit
- modify one field
- modify multiple fields
- save
- cancel
- validation
- persistence after refresh

## 12.5 Delete Project

Use only disposable project data.

Test:

- delete action
- confirmation
- cancel confirmation
- confirmed delete
- appearance in Deleted tab if this is the actual behavior
- dependency restriction if project has tasks/time entries
- error handling

## 12.6 Tasks Tab

Open the Tasks tab and dynamically inspect all controls and columns.

Generate complete tests for every actual function, such as:

- task listing
- create task
- edit task
- assignment
- project association
- status
- validation
- delete/restore if available
- empty state

Only generate tests for observed functions.

## 12.7 Deleted Tab

Inspect actual deleted-items workflow.

Test applicable behavior:

- deleted project/task listing
- count
- restore
- permanent deletion if available
- permissions
- empty state
- restored item persistence

Do not permanently delete real business data.

---

# 13. Page 8 — Reports

**Route:** `/reports`

## Visible Report Types

- Employee Report
- Project Report
- Time Summary
- Screenshots Report
- Idle Time Report
- Custom Report

Each card contains **Open Report**.

## 13.1 Reports Landing Page

Verify:

- all report cards visible
- title/description correct
- Open Report button visible
- route active
- cards navigate to the correct report

## 13.2 Employee Report

Open report and inspect actual controls.

Generate tests for discovered:

- employee selection
- date range
- filters
- totals
- activity/time values
- empty state
- export/download if present
- invalid filter/date combinations

## 13.3 Project Report

Inspect and test:

- project selection
- time totals
- employee/project relationship
- date filtering
- empty state
- data consistency
- export if available

## 13.4 Time Summary

Inspect and test actual metrics including, where present:

- tracked time
- idle time
- manual time
- totals
- filters
- date range
- empty state
- consistency

## 13.5 Screenshots Report

Inspect and test:

- filters
- screenshot/capture totals
- employee/team scope
- date range
- empty state
- report data consistency

## 13.6 Idle Time Report

Inspect and test:

- inactivity data
- employee filters
- project filters if present
- date range
- totals
- sorting/filtering if present
- empty state

## 13.7 Custom Report

Inspect actual builder and generate tests for:

- available filters
- valid combinations
- invalid combinations
- required fields
- no-result combinations
- generation
- reset/clear
- export/download if available

Report subpages were not supplied in the screenshots, so all internal controls must be discovered dynamically and marked `TO_BE_VERIFIED` until inspected.

---

# 14. Page 9 — Settings

**Route:** `/settings`

## Visible Tabs

- General
- Tracking
- Screenshots
- Permissions
- Working Hours
- Email
- Danger Zone

## General Tab Visible Controls

- Organization name
- Timezone
- Save Changes

## 14.1 General

Test safely:

- organization name visible
- current timezone visible
- timezone dropdown options
- Save Changes button
- required-field behavior
- whitespace
- long organization name
- unchanged save
- validation/error handling

Avoid changing real settings unless changes are reversible and explicitly safe.

## 14.2 Tracking

Open tab and inspect all fields.

Generate tests for:

- default values
- editable controls
- validation
- save
- cancel/reset if present
- persistence
- permissions
- error response

## 14.3 Screenshots Settings

Inspect and test actual screenshot policy controls:

- enabled/disabled controls
- interval or policy values if present
- validation
- save
- persistence
- authorization

Do not assume exact policy fields.

## 14.4 Permissions

Inspect and test:

- available permission settings
- role/user controls
- enable/disable behavior
- save
- validation
- authorization
- persistence

## 14.5 Working Hours

Inspect and test actual controls:

- schedule/day selection
- start/end values
- invalid ranges
- save
- persistence
- timezone interaction if relevant

## 14.6 Email

Inspect and test actual email-related configuration:

- fields
- validation
- save
- error handling
- permissions

## 14.7 Danger Zone

Treat as high-risk.

Inspect functionality but **do not execute destructive actions against production organization data**.

Generate cases for:

- visibility
- authorization
- confirmation requirements
- warning text
- cancel flow

Any irreversible test must be `BLOCKED` unless a disposable organization/environment is available.

---

# 15. Page 10 — Audit Logs

**Route:** `/audit`

## Visible Functionality

- action filter
- user filter
- From date
- To date
- search logs
- Refresh
- audit result area
- empty state

The UI states that administrative changes include membership, roles, projects, tasks, and organization settings, while timer start/stop is recorded elsewhere.

## 15.1 Page Load

Verify:

- route
- heading
- filter controls
- date controls
- search
- Refresh
- result region

## 15.2 Action Filter

Test:

- All actions
- each discovered action type
- result correctness
- no-result state
- action + user combination

## 15.3 User Filter

Test:

- All users
- individual users
- result correctness
- user + action combination

## 15.4 Date Range

Test:

- same-day
- multi-day
- past/current period
- start after end
- no-result period
- date + action + user combination

## 15.5 Search Logs

Test:

- exact matching term
- partial term
- case handling
- no-result
- special characters
- clear search
- search combined with filters

## 15.6 Audit Integrity

Where safe test mutations are performed elsewhere:

- verify corresponding audit record appears
- verify actor
- action
- target
- date/time
- details if available

Do not assume timer start/stop should appear here.

---

# 16. Page 11 — Profile

**Route:** `/profile`

## Visible Functionality

Profile summary:

- initials/avatar
- administrator name
- email
- owner role
- joined date

Profile form:

- Full Name
- Email
- Save Changes

Password section:

- New Password
- minimum 8-character hint
- Change Password

The UI states that email is the sign-in email and cannot be changed here.

## 16.1 Profile Load

Verify:

- summary card values
- form values
- role
- joined date format
- email consistency across summary/form
- email read-only behavior

## 16.2 Full Name

Using safe/reversible data only:

- valid name
- empty name
- whitespace
- leading/trailing spaces
- long name
- special characters
- Unicode
- unchanged save
- successful save
- persistence after refresh

Restore original name after test if modified.

## 16.3 Email

Verify:

- value present
- cannot be edited if UI says read-only
- no hidden edit flow exposed through normal UI

## 16.4 Change Password

High-risk test.

Generate validation tests for:

- empty password
- less than 8 characters
- exactly 8
- greater than 8
- special characters
- long password
- successful change only in a safe dedicated test account/environment

Do **not** change the real admin password unless explicitly authorized and a restoration/recovery plan exists.

Otherwise mark successful password mutation as `BLOCKED`.

## 16.5 Session Impact

If password change is safely executed, verify actual stated behavior:

- current session stays signed in
- other sessions are signed out

Only execute when safe.

---

# 17. Global Navigation

From every authenticated page, verify:

- Dashboard
- Users
- Live Activity
- Time History
- Screenshots
- Projects & Tasks
- Reports
- Settings
- Audit Logs
- Profile
- Log out

For each navigation action:

- link visible
- click works
- correct route
- correct page heading
- active state updates
- no unexpected full-app error
- browser Back/Forward behaves reasonably

---

# 18. Logout

Accessible from authenticated sidebar.

## 18.1 Functional Logout

Verify:

- Log out action works
- user returns to login page or expected unauthenticated route
- authenticated content is no longer accessible
- protected route redirects/rejects
- browser Back does not restore usable authenticated content
- re-login works

## 18.2 Session

Verify:

- session/token invalidated according to application behavior
- refresh after logout remains unauthenticated
- direct `/dashboard` after logout is blocked

---

# 19. Cross-Page Data Consistency

Where possible compare related data across pages.

Examples:

```text
Dashboard Team Members ↔ Users
Dashboard Tracking Now ↔ Live Activity
Dashboard Hours ↔ Time History / Reports
Dashboard Active Projects ↔ Projects & Tasks
Projects ↔ Project Report
Users ↔ Audit Logs after safe administrative changes
Settings changes ↔ Audit Logs
```

Do not fail a test solely on data that is known to be eventually consistent unless expected timing is defined.

---

# 20. API / Network / Console Validation

During browser tests capture relevant diagnostics when available:

- 4xx API responses
- 5xx API responses
- failed XHR/fetch
- timeout
- malformed/empty response
- JavaScript console errors
- unhandled promise errors
- failed resource loads

Associate errors with the active test case.

Do not mark every harmless browser warning as an application defect.

---

# 21. Failure Evidence

For every failed test record:

```text
timestamp
run_id
test_id
page
route
functionality
scenario
browser
executed_action
expected_result
actual_result
failure_classification
screenshot
console_errors
network_api_errors
exception
```

Failure classifications:

```text
APPLICATION_BUG
VALIDATION_FAILURE
AUTHENTICATION_ERROR
AUTHORIZATION_ERROR
API_ERROR
NETWORK_ERROR
LOCATOR_ERROR
TIMEOUT
TEST_DATA_ERROR
AUTOMATION_ERROR
AI_PLAN_ERROR
ENVIRONMENT_ERROR
UNKNOWN
```

---

# 22. Execution Priorities

## P0 / Smoke

At minimum include:

- login with valid admin credentials
- Dashboard loads
- all sidebar routes open
- Users loads
- Live Activity loads
- Time History loads
- Screenshots loads
- Projects & Tasks loads
- Reports loads
- Settings loads
- Audit Logs loads
- Profile loads
- Logout works

## P1 / Core Functional

Include:

- Login negative/validation
- Dashboard cards/sections/navigation
- Users search/filter/Add User/Actions
- Live Activity refresh/auto-refresh
- Time History filters/date range
- Screenshots filters/date range
- Projects tabs and safe CRUD
- all report cards and report subpages
- each Settings tab
- Audit filters/search
- Profile edit validation

## P2 / Extended

Include:

- boundary inputs
- browser navigation
- combined filters
- API failure/recovery scenarios
- responsive functional checks
- permission/RBAC checks where additional roles are available

---

# 23. Completion Rule

Do not consider TrackPilot testing complete until all of these top-level areas are covered:

```text
Login
Dashboard
Users
Live Activity
Time History
Screenshots
Projects & Tasks
    Projects
    Tasks
    Deleted
Reports
    Employee Report
    Project Report
    Time Summary
    Screenshots Report
    Idle Time Report
    Custom Report
Settings
    General
    Tracking
    Screenshots
    Permissions
    Working Hours
    Email
    Danger Zone
Audit Logs
Profile
Logout
Global Navigation
Session
Cross-page data consistency
Failure/API/console evidence
```

Nested report/settings/project controls that were not visible in the supplied screenshots must be discovered dynamically during execution.

Do not silently skip them.

Use:

```text
TO_BE_VERIFIED
BLOCKED
NOT_APPLICABLE
```

where appropriate.
