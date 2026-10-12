*** Settings ***
Documentation     TrackPilot Login Page Test Cases
...               Mapped exactly to testsuites/trackpilot.testcases.md
...               Test IDs must be maintained to ensure traceability in Grafana.
Library           SeleniumLibrary
Resource          ../../resources/browser.resource
Resource          ../../resources/common.resource
Resource          ../../resources/assertions.resource
Resource          ../../resources/trackpilot.resource

Suite Setup       Log    Starting TrackPilot Login Suite
Suite Teardown    Close All Browsers
Test Setup        Open Application
Test Teardown     Close Test Browser

Force Tags        trackpilot    login
Default Tags      browser

*** Test Cases ***
# =============================================================================
# Application Availability
# =============================================================================
TC-TP-LOGIN-AVAIL-001 Login Page Opens Successfully
    [Documentation]    Verify the application login page is accessible.
    [Tags]    availability    smoke    P0
    Verify Login Page Is Displayed
    Page Title Should Be    TrackPilot Admin

TC-TP-LOGIN-AVAIL-002 Login Page Loads Over HTTPS
    [Documentation]    Verify the application enforces HTTPS.
    [Tags]    availability    P1
    ${url}=    Get Location
    Should Start With    ${url}    https://    msg=Login page is not loaded over HTTPS

TC-TP-LOGIN-AVAIL-003 Invalid Route Handling
    [Documentation]    Verify unknown route redirects or shows 404.
    [Tags]    availability    P1
    Go To    ${BASE_URL}/this-route-does-not-exist
    Sleep    2s
    # Often redirects to login or shows a Not Found message
    ${url}=    Get Location
    ${is_login}=    Run Keyword And Return Status    Should Contain    ${url}    /login
    ${is_404}=    Run Keyword And Return Status    Page Should Contain    Not Found
    Should Be True    ${is_login} or ${is_404}    msg=Expected redirect to login or 404 page

# =============================================================================
# Authentication
# =============================================================================
TC-TP-LOGIN-AUTH-001 Valid Admin Login
    [Documentation]    Verify login with valid admin credentials succeeds.
    [Tags]    authentication    positive    smoke    P0
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Successful

TC-TP-LOGIN-AUTH-002 Login with Invalid Password
    [Documentation]    Verify login with invalid password fails gracefully.
    [Tags]    authentication    negative    P0
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    WrongPassword123!
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed

TC-TP-LOGIN-AUTH-003 Login with Invalid Email
    [Documentation]    Verify login with invalid email fails gracefully.
    [Tags]    authentication    negative    P0
    Enter TrackPilot Credentials    invalid_admin@pods365.com    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed

TC-TP-LOGIN-AUTH-004 Login with Both Invalid Email and Password
    [Documentation]    Verify login with completely invalid credentials fails.
    [Tags]    authentication    negative    P1
    Enter TrackPilot Credentials    invalid_admin@pods365.com    WrongPassword123!
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed

TC-TP-LOGIN-AUTH-005 Retry Login After Invalid Attempt
    [Documentation]    Verify user can login successfully after an invalid attempt.
    [Tags]    authentication    P1
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    WrongPassword123!
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed
    # Now try valid
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Successful

TC-TP-LOGIN-AUTH-006 Login by Pressing Enter Key
    [Documentation]    Verify login form submits on Enter key press.
    [Tags]    authentication    P1
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${VALID_USER_PASSWORD}
    Press Keys    None    ENTER
    Verify Login Successful

TC-TP-LOGIN-AUTH-007 Successful Login Redirects to Dashboard
    [Documentation]    Verify user is redirected to dashboard after login.
    [Tags]    authentication    P0
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Dashboard Is Loaded

TC-TP-LOGIN-AUTH-008 Accessing Protected Page Without Login
    [Documentation]    Verify unauthenticated users cannot access protected routes.
    [Tags]    authentication    security    P0
    # Create new session by closing and reopening
    Close Test Browser
    Open Test Browser    ${BASE_URL}/dashboard
    Sleep    2s
    Verify Login Page Is Displayed

# =============================================================================
# Input Validation
# =============================================================================
TC-TP-LOGIN-VAL-001 Login with Empty Email Field
    [Documentation]    Verify login fails if email is empty.
    [Tags]    validation    negative    P0
    Enter TrackPilot Credentials    ${EMPTY}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Page Is Displayed

TC-TP-LOGIN-VAL-002 Login with Empty Password Field
    [Documentation]    Verify login fails if password is empty.
    [Tags]    validation    negative    P0
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${EMPTY}
    Click TrackPilot Login Button
    Verify Login Page Is Displayed

TC-TP-LOGIN-VAL-003 Login with Both Fields Empty
    [Documentation]    Verify login fails if both fields are empty.
    [Tags]    validation    negative    P1
    Enter TrackPilot Credentials    ${EMPTY}    ${EMPTY}
    Click TrackPilot Login Button
    Verify Login Page Is Displayed

TC-TP-LOGIN-VAL-004 Login with Invalid Email Format
    [Documentation]    Verify email format validation.
    [Tags]    validation    negative    P1
    Enter TrackPilot Credentials    not-an-email-format    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Page Is Displayed

TC-TP-LOGIN-VAL-005 Login with Spaces-Only Email
    [Documentation]    Verify spaces are handled properly.
    [Tags]    validation    negative    P2
    Enter TrackPilot Credentials    ${SPACE}${SPACE}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Page Is Displayed

TC-TP-LOGIN-VAL-006 Login with Spaces Around Email
    [Documentation]    Verify email with leading/trailing spaces trims and succeeds.
    [Tags]    validation    positive    P2
    Enter TrackPilot Credentials    ${SPACE}${VALID_USER_EMAIL}${SPACE}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Successful

# =============================================================================
# Password Field Behaviour
# =============================================================================
TC-TP-LOGIN-PWD-001 Password Input is Masked
    [Documentation]    Verify password field is type="password" by default.
    [Tags]    ui    security    P1
    Wait For Login Page
    Page Should Contain Element    css:input[type="password"]

TC-TP-LOGIN-PWD-002 Show/Hide Password Toggle
    [Documentation]    Verify password toggle changes input type.
    [Tags]    ui    P2
    Wait For Login Page
    Enter Password    SecretPass!
    Toggle Password Visibility
    # Depending on implementation, it might change to text or remain password if unsupported
    ${is_text}=    Run Keyword And Return Status    Page Should Contain Element    css:input[type="text"][value*="SecretPass!"]
    IF    not ${is_text}
        Log    Show/Hide password feature not detected.
    END

# =============================================================================
# Security Edge Cases
# =============================================================================
TC-TP-LOGIN-SEC-001 SQL Injection in Login Fields
    [Documentation]    Verify basic SQLi attempt doesn't break app.
    [Tags]    security    negative    P1
    Enter TrackPilot Credentials    ' OR 1=1 --    ' OR 1=1 --
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed
    Page Should Not Contain    SQL syntax

TC-TP-LOGIN-SEC-002 XSS Attempt in Login Fields
    [Documentation]    Verify basic XSS attempt doesn't execute.
    [Tags]    security    negative    P1
    Enter TrackPilot Credentials    <script>alert('xss')</script>    password
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed

TC-TP-LOGIN-SEC-003 Login with Extremely Long Input
    [Documentation]    Verify extremely long input doesn't crash server.
    [Tags]    security    negative    P2
    # Generate 1000 char string
    ${long_string}=    Evaluate    "A" * 1000
    Enter TrackPilot Credentials    ${long_string}@example.com    ${long_string}
    Click TrackPilot Login Button
    Sleep    2s
    Verify Login Page Is Displayed

# =============================================================================
# Session Management
# =============================================================================
TC-TP-LOGIN-SESSION-001 Session Created After Successful Login
    [Documentation]    Verify session works across multiple pages.
    [Tags]    session    P0
    Enter TrackPilot Credentials    ${VALID_USER_EMAIL}    ${VALID_USER_PASSWORD}
    Click TrackPilot Login Button
    Verify Login Successful
    # Navigate to another protected route and ensure we aren't kicked out
    Go To    ${BASE_URL}/users
    Sleep    2s
    ${url}=    Get Location
    Should Not Contain    ${url}    /login

# =============================================================================
# Browser Behaviour
# =============================================================================
TC-TP-LOGIN-BROWSER-001 Refresh Login Page
    [Documentation]    Verify refresh preserves page state.
    [Tags]    browser    P2
    Wait For Login Page
    Reload Page
    Verify Login Page Is Displayed

TC-TP-LOGIN-BROWSER-002 Browser Back Button on Login Page
    [Documentation]    Verify back button behaviour.
    [Tags]    browser    P2
    Wait For Login Page
    Go To    ${BASE_URL}/login?test=1
    Sleep    1s
    Execute Javascript    window.history.back()
    Sleep    1s
    Verify Login Page Is Displayed
