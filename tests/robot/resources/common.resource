*** Settings ***
Documentation     Common high-level keywords for the application under test.
...               Provides business-level actions like "Login As Valid User"
...               that abstract away locator details and page interactions.
...               This abstraction layer prepares for future AI action mapping.
Library           SeleniumLibrary
Variables         ../variables/common.py
Resource          browser.resource


*** Keywords ***
Open Application
    [Documentation]    Opens the application login page in a test browser.
    Open Test Browser    url=${BASE_URL}/login

Login As Valid User
    [Documentation]    Logs in with valid credentials from environment variables.
    Wait For Login Page
    Enter Email    ${VALID_USER_EMAIL}
    Enter Password    ${VALID_USER_PASSWORD}
    Click Login Button

Login With Credentials
    [Documentation]    Logs in with specified credentials.
    [Arguments]    ${email}    ${password}
    Wait For Login Page
    Enter Email    ${email}
    Enter Password    ${password}
    Click Login Button

Wait For Login Page
    [Documentation]    Waits until the login form is visible.
    ${found}=    Run Keyword And Return Status
    ...    Wait Until Element Is Visible    css:[data-testid="email-input"]    timeout=${SHORT_TIMEOUT}
    IF    not ${found}
        ${found}=    Run Keyword And Return Status
        ...    Wait Until Element Is Visible    id:email    timeout=${SHORT_TIMEOUT}
    END
    IF    not ${found}
        ${found}=    Run Keyword And Return Status
        ...    Wait Until Element Is Visible    id:username    timeout=${SHORT_TIMEOUT}
    END
    IF    not ${found}
        Wait Until Element Is Visible    css:input[type="email"]    timeout=${DEFAULT_TIMEOUT}
    END

Enter Email
    [Documentation]    Types the email into the email/username field.
    [Arguments]    ${email}
    ${done}=    Try Input Text    css:[data-testid="email-input"]    ${email}
    IF    not ${done}
        ${done}=    Try Input Text    id:email    ${email}
    END
    IF    not ${done}
        ${done}=    Try Input Text    id:username    ${email}
    END
    IF    not ${done}
        Try Input Text    css:input[type="email"]    ${email}
    END

Enter Password
    [Documentation]    Types the password into the password field.
    [Arguments]    ${password}
    ${done}=    Try Input Text    css:[data-testid="password-input"]    ${password}
    IF    not ${done}
        ${done}=    Try Input Text    id:password    ${password}
    END
    IF    not ${done}
        Try Input Text    css:input[type="password"]    ${password}
    END

Try Input Text
    [Documentation]    Tries to input text into a field. Returns True if successful.
    [Arguments]    ${locator}    ${text}
    ${visible}=    Run Keyword And Return Status
    ...    Wait Until Element Is Visible    ${locator}    timeout=${SHORT_TIMEOUT}
    IF    ${visible}
        Clear Element Text    ${locator}
        Input Text    ${locator}    ${text}
        RETURN    ${TRUE}
    END
    RETURN    ${FALSE}

Click Login Button
    [Documentation]    Clicks the login/sign-in button.
    ${clicked}=    Run Keyword And Return Status
    ...    Click Element    css:[data-testid="login-button"]
    IF    not ${clicked}
        ${clicked}=    Run Keyword And Return Status
        ...    Click Element    css:button[type="submit"]
    END
    IF    not ${clicked}
        ${clicked}=    Run Keyword And Return Status
        ...    Click Element    xpath://button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign in') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'login')]
    END
    IF    not ${clicked}
        Click Button    Login
    END

Logout User
    [Documentation]    Logs out the current user.
    ${clicked}=    Run Keyword And Return Status
    ...    Click Element    css:[data-testid="logout-button"]
    IF    not ${clicked}
        ${clicked}=    Run Keyword And Return Status
        ...    Click Element    xpath://button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign out')]
    END
    IF    not ${clicked}
        # Try clicking any logout-looking link
        ${clicked}=    Run Keyword And Return Status
        ...    Click Element    xpath://a[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign out')]
    END

Verify Login Successful
    [Documentation]    Verifies that login was successful.
    ...                Checks that we are no longer on the login page.
    Sleep    2s    Wait for redirect
    ${url}=    Get Location
    Should Not Contain    ${url}    /login    msg=Login failed — still on login page

Verify Login Page Is Displayed
    [Documentation]    Verifies we are on the login page.
    ${url}=    Get Location
    Should Contain    ${url}    /login    msg=Not on login page
