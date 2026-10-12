*** Settings ***
Documentation     TrackPilot specific resource file.
...               Provides high-level actions interacting with TrackPilot UI.
Library           SeleniumLibrary
Variables         ../variables/common.py
Resource          browser.resource
Resource          common.resource

*** Keywords ***
# =============================================================================
# TrackPilot Login Page Actions
# =============================================================================
Go To TrackPilot Login Page
    [Documentation]    Navigates to the TrackPilot login page directly.
    Go To    ${BASE_URL}/login
    Wait For Login Page

Enter TrackPilot Credentials
    [Arguments]    ${email}    ${password}
    Enter Email    ${email}
    Enter Password    ${password}

Click TrackPilot Login Button
    Click Login Button

Toggle Password Visibility
    [Documentation]    Clicks the show/hide password toggle if available.
    ${found}=    Run Keyword And Return Status
    ...    Wait Until Element Is Visible    css:button[aria-label*="password"]    timeout=${SHORT_TIMEOUT}
    IF    ${found}
        Click Element    css:button[aria-label*="password"]
    ELSE
        Log    Password toggle not found or not supported.    level=WARN
    END

# =============================================================================
# Validation & States
# =============================================================================
Verify Invalid Credentials Error
    [Documentation]    Verifies an error message is shown for invalid login.
    Wait Until Page Contains    Invalid credentials    timeout=${SHORT_TIMEOUT}

Verify Field Required Error
    [Documentation]    Verifies a required field validation error is shown.
    ${found}=    Run Keyword And Return Status    Wait Until Page Contains    Required    timeout=${SHORT_TIMEOUT}
    IF    not ${found}
        Wait Until Page Contains    Please fill out this field    timeout=${SHORT_TIMEOUT}
    END

Verify Dashboard Is Loaded
    [Documentation]    Verifies successful redirect to the dashboard.
    Sleep    2s    Wait for redirect to complete
    ${url}=    Get Location
    Should Contain    ${url}    /dashboard    msg=Expected to be on /dashboard, but was on ${url}
