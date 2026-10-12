*** Settings ***
Documentation     Browser management resource.
...               Provides reusable keywords for opening, configuring,
...               and closing browser sessions via remote Selenium.
...               Tests should use these keywords — never put Selenium
...               URLs or browser config directly in test files.
Library           SeleniumLibrary    timeout=${DEFAULT_TIMEOUT}
Variables         ../variables/common.py


*** Keywords ***
Open Test Browser
    [Documentation]    Opens a remote Chrome browser via Selenium container.
    ...                Reads configuration from environment variables.
    [Arguments]    ${url}=${BASE_URL}
    Open Browser
    ...    url=${url}
    ...    browser=${BROWSER}
    ...    remote_url=${SELENIUM_REMOTE_URL}
    ...    options=add_argument("--no-sandbox");add_argument("--disable-dev-shm-usage");add_argument("--disable-gpu")
    Set Window Size    1920    1080
    Set Selenium Timeout    ${DEFAULT_TIMEOUT}

Close Test Browser
    [Documentation]    Closes all browser windows and ends the session.
    Close All Browsers

Capture Failure Screenshot
    [Documentation]    Captures a screenshot on failure with a predictable name.
    [Arguments]    ${test_id}=unknown
    Capture Page Screenshot    ${SCREENSHOT_DIR}/${test_id}_{index}.png
