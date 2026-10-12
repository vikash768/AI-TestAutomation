*** Settings ***
Documentation     Assertion keywords for verifying application state.
...               Provides readable assertion wrappers used by test suites.
Library           SeleniumLibrary
Variables         ../variables/common.py


*** Keywords ***
Page Should Contain Text
    [Documentation]    Asserts that the page body contains the given text.
    [Arguments]    ${text}
    Wait Until Page Contains    ${text}    timeout=${DEFAULT_TIMEOUT}

Page Should Not Contain Text
    [Documentation]    Asserts that the page body does NOT contain the given text.
    [Arguments]    ${text}
    Page Should Not Contain    ${text}

Element Should Be Visible On Page
    [Documentation]    Asserts an element is visible.
    [Arguments]    ${locator}
    Wait Until Element Is Visible    ${locator}    timeout=${DEFAULT_TIMEOUT}

Element Should Not Be Visible On Page
    [Documentation]    Asserts an element is NOT visible.
    [Arguments]    ${locator}
    Wait Until Element Is Not Visible    ${locator}    timeout=${DEFAULT_TIMEOUT}

Current URL Should Contain
    [Documentation]    Asserts the current URL contains the given substring.
    [Arguments]    ${expected}
    ${url}=    Get Location
    Should Contain    ${url}    ${expected}

Current URL Should Not Contain
    [Documentation]    Asserts the current URL does NOT contain the given substring.
    [Arguments]    ${unexpected}
    ${url}=    Get Location
    Should Not Contain    ${url}    ${unexpected}

Page Title Should Be
    [Documentation]    Asserts the page title matches.
    [Arguments]    ${expected_title}
    Title Should Be    ${expected_title}

Element Text Should Be
    [Documentation]    Asserts that an element's text matches.
    [Arguments]    ${locator}    ${expected_text}
    Wait Until Element Is Visible    ${locator}    timeout=${DEFAULT_TIMEOUT}
    Element Text Should Be    ${locator}    ${expected_text}

Element Text Should Contain
    [Documentation]    Asserts that an element's text contains a substring.
    [Arguments]    ${locator}    ${expected_substring}
    Wait Until Element Is Visible    ${locator}    timeout=${DEFAULT_TIMEOUT}
    ${text}=    Get Text    ${locator}
    Should Contain    ${text}    ${expected_substring}
