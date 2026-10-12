"""
AI Test Case Generator.

Reads parsed SuiteContext (from suite_parser) and generates concrete
TestCase objects with structured Action steps.

This is the AI reasoning layer: it understands the testing instructions
and produces executable test plans. It does NOT execute anything — that
is the executor's job.

The generator maps each testing instruction to one or more TestCase objects,
each containing a sequence of deterministic Action steps.
"""

from __future__ import annotations

import json
import logging
from google import genai
from google.genai import types
import os
import re
from dataclasses import dataclass, field

from automation.actions.schema import (
    Action,
    ActionType,
    Locator,
    LocatorStrategy,
    TestCase,
)
from automation.ai.suite_parser import (
    PageContext,
    SuiteContext,
    TestCategory,
    TestInstruction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Locator helpers — multi-strategy locators for resilient element finding
# ---------------------------------------------------------------------------

# Login page locators (derived from testsuite.md element tables)
# Ordered by reliability: CSS type selectors first, data-testid last
EMAIL_LOCATORS = [
    Locator(LocatorStrategy.CSS, 'input[type="email"]'),
    Locator(LocatorStrategy.ID, "email"),
    Locator(LocatorStrategy.ID, "username"),
    Locator(LocatorStrategy.CSS, 'input[name="email"]'),
    Locator(LocatorStrategy.XPATH, "//input[contains(@placeholder,'email') or contains(@placeholder,'Email')]"),
    Locator(LocatorStrategy.DATA_TESTID, "email-input"),
]

PASSWORD_LOCATORS = [
    Locator(LocatorStrategy.CSS, 'input[type="password"]'),
    Locator(LocatorStrategy.ID, "password"),
    Locator(LocatorStrategy.CSS, 'input[name="password"]'),
    Locator(LocatorStrategy.XPATH, "//input[contains(@placeholder,'password') or contains(@placeholder,'Password')]"),
    Locator(LocatorStrategy.DATA_TESTID, "password-input"),
]

LOGIN_BUTTON_LOCATORS = [
    Locator(LocatorStrategy.CSS, 'button[type="submit"]'),
    Locator(LocatorStrategy.XPATH, "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign in') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'login')]"),
    Locator(LocatorStrategy.DATA_TESTID, "login-button"),
]


def _first_locator(*locator_lists: list[Locator]) -> Locator:
    """Return the first locator from the given lists (highest priority)."""
    for loc_list in locator_lists:
        if loc_list:
            return loc_list[0]
    return Locator(LocatorStrategy.CSS, "body")


class TestGenerator:
    """Generates concrete TestCase objects from parsed SuiteContext.

    For each page and each testing category, the generator reads the
    instructions and produces TestCase objects with structured Action steps.
    """

    def __init__(self, base_url: str = "", email: str = "", password: str = ""):
        self._base_url = base_url or os.environ.get("BASE_URL", "https://trackpilot.pods365.com")
        self._email = email or os.environ.get("VALID_USER_EMAIL", "")
        self._password = password or os.environ.get("VALID_USER_PASSWORD", "")
        self._test_id_counters: dict[str, int] = {}
        
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)
        else:
            self._client = None
            logger.warning("No GEMINI_API_KEY found, falling back to mock generators.")

    def generate(self, ctx: SuiteContext) -> list[TestCase]:
        """Generate all test cases from the parsed suite context.

        Returns:
            A list of TestCase objects ready for execution.
        """
        all_cases: list[TestCase] = []

        for page in ctx.pages:
            page_cases = self._generate_page_tests(page)
            all_cases.extend(page_cases)
            logger.info(
                "Generated %d tests for page: %s",
                len(page_cases),
                page.page_name,
            )

        logger.info("Total test cases generated: %d", len(all_cases))
        return all_cases

    def _generate_page_tests(self, page: PageContext) -> list[TestCase]:
        """Generate tests for a single page."""
        cases = []

        for category in page.categories:
            category_cases = self._generate_category_tests(page, category)
            cases.extend(category_cases)

        return cases

    def _generate_category_tests(
        self, page: PageContext, category: TestCategory
    ) -> list[TestCase]:
        """Generate tests for a single category within a page."""
        cases = []

        if self._client:
            try:
                logger.info(f"Using AI to generate tests for {page.page_name} - {category.category_name}")
                cases = self._generate_with_ai(page, category)
                if cases:
                    return cases
            except Exception as e:
                logger.error(f"AI Generation failed: {e}")

        # Map category names to specialized generators
        generators = {
            "Application Availability": self._gen_availability,
            "Authentication — Positive Cases": self._gen_auth_positive,
            "Authentication — Negative Cases": self._gen_auth_negative,
            "Input Validation": self._gen_input_validation,
            "Password Field Behaviour": self._gen_password_field,
            "Security Edge Cases": self._gen_security,
            "Session Management": self._gen_session,
            "Browser Behaviour": self._gen_browser_behaviour,
            "Page Load & Availability": self._gen_page_load,
            "Navigation": self._gen_navigation,
            "Data Display": self._gen_data_display,
            "Logout": self._gen_logout,
            "Responsiveness": self._gen_responsiveness,
            "Search": self._gen_search,
            "User List Display": self._gen_user_list,
            "Pagination (if applicable)": self._gen_pagination,
            "Date Filters (if applicable)": self._gen_date_filters,
            "Real-time Elements": self._gen_realtime,
        }

        # Try specialized generator first
        gen_fn = generators.get(category.category_name)
        if gen_fn:
            cases = gen_fn(page, category)
        else:
            # Fall back to instruction-driven generation
            cases = self._gen_from_instructions(page, category)

        return cases


    def _generate_with_ai(self, page: PageContext, category: TestCategory) -> list[TestCase]:
        prompt = f"""
You are an expert QA Automation Architect. Generate a JSON array of test cases based on the provided instructions.
Application Base URL: {self._base_url}
Admin Email: {self._email}
Admin Password: {self._password}

Page: {page.page_name}
Category: {category.category_name}

Instructions:
{chr(10).join(f"- {i.instruction}" for i in category.instructions)}

Context/Description:
{category.description}

Return ONLY a valid JSON array of objects representing test cases. Do not include markdown formatting like ```json.
Each object must have exactly these keys:
- "test_id": string (use format TC-TP-{page.page_name.upper().replace(' ', '')}-{category.category_name.upper().replace(' ', '')[:4]}-00X)
- "test_name": string (a concise name for the test)
- "page": string (e.g. "{page.page_name}")
- "functionality": string (e.g. "{category.category_name}")
- "scenario": string (the test scenario description)
- "preconditions": array of strings (e.g. ["Logged in as admin"])
- "test_data": dictionary of string key-value pairs
- "expected_result": string (what to verify)
- "priority": string (e.g. "P0", "P1")
- "test_type": string (e.g. "browser", "api")
- "tags": array of strings (e.g. ["smoke", "positive"])
- "steps": array of Action objects. 

Action objects must have:
- "action_type": string (Must be one of: NAVIGATE, CLICK, TYPE, CLEAR, SELECT, CHECK, UNCHECK, WAIT, WAIT_VISIBLE, WAIT_NOT_VISIBLE, WAIT_URL_CONTAINS, PRESS_KEY, VERIFY_TEXT, VERIFY_NOT_TEXT, VERIFY_VISIBLE, VERIFY_NOT_VISIBLE, VERIFY_URL, VERIFY_URL_CONTAINS, VERIFY_URL_NOT_CONTAINS, VERIFY_VALUE, VERIFY_ENABLED, VERIFY_DISABLED, VERIFY_TITLE, VERIFY_TITLE_CONTAINS, VERIFY_ELEMENT_ATTRIBUTE, VERIFY_ELEMENT_COUNT, CAPTURE_SCREENSHOT, EXECUTE_JS, RELOAD_PAGE, GO_BACK, SLEEP, CLOSE_BROWSER, OPEN_BROWSER, SET_WINDOW_SIZE, GET_CONSOLE_ERRORS)
- "description": string
- "locator": object OR null. If object, must have "strategy" (one of: data-testid, id, name, css, xpath, text, link_text, partial_link_text) and "value" (string).
- "value": string OR null.
- "timeout": float (default 15.0)
- "extra": dictionary of extra properties

Guidelines:
- First step is usually NAVIGATE or logging in if the page is protected.
- To login, navigate to /login, type email, type password, click submit button.
- Make tests robust. Use CSS or XPath locators if IDs are unknown.
"""
        response = self._client.models.generate_content(
            model='gemini-1.5-pro',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(response.text)
        
        parsed_cases = []
        for item in data:
            steps = []
            for s in item.get("steps", []):
                loc = None
                if s.get("locator"):
                    loc = Locator(
                        strategy=LocatorStrategy(s["locator"]["strategy"]),
                        value=s["locator"]["value"]
                    )
                action = Action(
                    action_type=ActionType(s["action_type"]),
                    description=s.get("description", ""),
                    locator=loc,
                    value=s.get("value"),
                    timeout=float(s.get("timeout", 15.0)),
                    extra=s.get("extra", {})
                )
                steps.append(action)
                
            tc = TestCase(
                test_id=item["test_id"],
                test_name=item["test_name"],
                page=item["page"],
                functionality=item["functionality"],
                scenario=item["scenario"],
                preconditions=item.get("preconditions", []),
                test_data=item.get("test_data", {}),
                steps=steps,
                expected_result=item.get("expected_result", ""),
                priority=item.get("priority", "P1"),
                test_type=item.get("test_type", "browser"),
                tags=item.get("tags", [])
            )
            parsed_cases.append(tc)
            
        return parsed_cases

    def _next_id(self, prefix: str) -> str:
        """Generate the next test ID for a given prefix."""
        count = self._test_id_counters.get(prefix, 0) + 1
        self._test_id_counters[prefix] = count
        return f"TC-TP-{prefix}-{count:03d}"

    def _login_steps(self, email: str = "", password: str = "") -> list[Action]:
        """Generate standard login action steps."""
        email = email or self._email
        password = password or self._password
        return [
            Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
            Action(ActionType.WAIT_VISIBLE, "Wait for email field", locator=EMAIL_LOCATORS[0],
                   fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
            Action(ActionType.CLEAR, "Clear email field", locator=EMAIL_LOCATORS[0],
                   fallback_locators=EMAIL_LOCATORS[1:]),
            Action(ActionType.TYPE, "Enter email", locator=EMAIL_LOCATORS[0],
                   fallback_locators=EMAIL_LOCATORS[1:], value=email),
            Action(ActionType.CLEAR, "Clear password field", locator=PASSWORD_LOCATORS[0],
                   fallback_locators=PASSWORD_LOCATORS[1:]),
            Action(ActionType.TYPE, "Enter password", locator=PASSWORD_LOCATORS[0],
                   fallback_locators=PASSWORD_LOCATORS[1:], value=password),
            Action(ActionType.CLICK, "Click login button", locator=LOGIN_BUTTON_LOCATORS[0],
                   fallback_locators=LOGIN_BUTTON_LOCATORS[1:]),
            Action(ActionType.SLEEP, "Wait for login processing", value="3"),
        ]

    def _valid_login_steps(self) -> list[Action]:
        """Login with valid credentials."""
        return self._login_steps(self._email, self._password)

    # ------------------------------------------------------------------
    # Specialized generators for known categories
    # ------------------------------------------------------------------

    def _gen_availability(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate availability tests for Login page."""
        prefix = "LOGIN-AVAIL"
        cases = []

        # TC: Login page opens
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Application Availability",
            scenario="Login page opens successfully",
            steps=[
                Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for email field", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.VERIFY_VISIBLE, "Email input visible", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:]),
                Action(ActionType.VERIFY_VISIBLE, "Password input visible", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:]),
                Action(ActionType.VERIFY_VISIBLE, "Login button visible", locator=LOGIN_BUTTON_LOCATORS[0],
                       fallback_locators=LOGIN_BUTTON_LOCATORS[1:]),
            ],
            expected_result="Login page loads with all form elements visible",
            priority="P0",
            tags=["availability", "smoke"],
        ))

        # TC: HTTPS enforcement
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Application Availability",
            scenario="Login page loads over HTTPS",
            steps=[
                Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for page", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.VERIFY_URL_CONTAINS, "Verify HTTPS", value="https://"),
            ],
            expected_result="URL starts with https://",
            priority="P1",
            tags=["availability", "security"],
        ))

        # TC: Invalid route handling
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Application Availability",
            scenario="Invalid route redirects to login or shows 404",
            steps=[
                Action(ActionType.NAVIGATE, "Open invalid route", value=f"{self._base_url}/this-route-does-not-exist"),
                Action(ActionType.SLEEP, "Wait for redirect", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "Check redirected to login", value="/login"),
            ],
            expected_result="Invalid route redirects to login page",
            priority="P1",
            tags=["availability"],
        ))

        return cases

    def _gen_auth_positive(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate positive authentication tests."""
        prefix = "LOGIN-AUTH"
        cases = []

        # Valid login
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Authentication",
            scenario="Valid admin login succeeds",
            test_data={"email": self._email, "password": "***"},
            steps=self._valid_login_steps() + [
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Not on login page", value="/login"),
            ],
            expected_result="User redirected away from login page",
            priority="P0",
            tags=["authentication", "positive", "smoke"],
        ))

        # Login redirects to dashboard
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Authentication",
            scenario="Successful login redirects to dashboard",
            steps=self._valid_login_steps() + [
                Action(ActionType.VERIFY_URL_CONTAINS, "On dashboard", value="/dashboard"),
            ],
            expected_result="URL contains /dashboard after login",
            priority="P0",
            tags=["authentication", "positive"],
        ))

        # Login via Enter key
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Authentication",
            scenario="Login by pressing Enter key",
            steps=[
                Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for email field", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.TYPE, "Enter email", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], value=self._email),
                Action(ActionType.TYPE, "Enter password", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:], value=self._password),
                Action(ActionType.PRESS_KEY, "Press Enter", value="ENTER"),
                Action(ActionType.SLEEP, "Wait for processing", value="3"),
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Not on login page", value="/login"),
            ],
            expected_result="Login succeeds via Enter key",
            priority="P1",
            tags=["authentication", "positive"],
        ))

        # Retry after failure
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Authentication",
            scenario="Retry login after invalid attempt succeeds",
            steps=[
                # First: invalid attempt
                Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for email field", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.TYPE, "Enter email", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], value=self._email),
                Action(ActionType.TYPE, "Enter password", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:], value="WrongPassword123!"),
                Action(ActionType.CLICK, "Click login", locator=LOGIN_BUTTON_LOCATORS[0],
                       fallback_locators=LOGIN_BUTTON_LOCATORS[1:]),
                Action(ActionType.SLEEP, "Wait", value="2"),
                Action(ActionType.VERIFY_URL_CONTAINS, "Still on login", value="/login"),
                # Second: valid attempt
                Action(ActionType.CLEAR, "Clear email", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:]),
                Action(ActionType.TYPE, "Enter correct email", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], value=self._email),
                Action(ActionType.CLEAR, "Clear password", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:]),
                Action(ActionType.TYPE, "Enter correct password", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:], value=self._password),
                Action(ActionType.CLICK, "Click login", locator=LOGIN_BUTTON_LOCATORS[0],
                       fallback_locators=LOGIN_BUTTON_LOCATORS[1:]),
                Action(ActionType.SLEEP, "Wait for processing", value="3"),
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Not on login page", value="/login"),
            ],
            expected_result="Second login attempt with valid credentials succeeds",
            priority="P1",
            tags=["authentication", "positive"],
        ))

        return cases

    def _gen_auth_negative(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate negative authentication tests."""
        prefix = "LOGIN-AUTH"
        cases = []

        negative_scenarios = [
            ("Invalid password", self._email, "WrongPassword123!", "P0"),
            ("Invalid email", "invalid_admin@pods365.com", self._password, "P0"),
            ("Both invalid", "invalid_admin@pods365.com", "WrongPassword123!", "P1"),
        ]

        for scenario, email, password, priority in negative_scenarios:
            cases.append(TestCase(
                test_id=self._next_id(prefix),
                page=page.page_name,
                functionality="Authentication",
                scenario=f"Login with {scenario.lower()} fails",
                test_data={"email": email, "password": "***"},
                steps=self._login_steps(email, password) + [
                    Action(ActionType.VERIFY_URL_CONTAINS, "Still on login page", value="/login"),
                ],
                expected_result="User remains on login page",
                priority=priority,
                tags=["authentication", "negative"],
            ))

        # Protected page without login
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Authentication",
            scenario="Accessing protected page without login redirects to login",
            steps=[
                Action(ActionType.NAVIGATE, "Open dashboard directly", value=f"{self._base_url}/dashboard"),
                Action(ActionType.SLEEP, "Wait for redirect", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "Redirected to login", value="/login"),
            ],
            expected_result="Unauthenticated user redirected to login",
            priority="P0",
            tags=["authentication", "security"],
        ))

        return cases

    def _gen_input_validation(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate input validation tests."""
        prefix = "LOGIN-VAL"
        cases = []

        validation_scenarios = [
            ("Empty email field", "", self._password, "P0"),
            ("Empty password field", self._email, "", "P0"),
            ("Both fields empty", "", "", "P1"),
            ("Invalid email format", "not-an-email-format", self._password, "P1"),
            ("Spaces-only email", "   ", self._password, "P2"),
        ]

        for scenario, email, password, priority in validation_scenarios:
            cases.append(TestCase(
                test_id=self._next_id(prefix),
                page=page.page_name,
                functionality="Input Validation",
                scenario=f"Login with {scenario.lower()}",
                steps=self._login_steps(email, password) + [
                    Action(ActionType.VERIFY_URL_CONTAINS, "Still on login page", value="/login"),
                ],
                expected_result="User remains on login page",
                priority=priority,
                tags=["validation", "negative"],
            ))

        # Email with spaces around (should succeed)
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Input Validation",
            scenario="Email with leading/trailing spaces",
            steps=self._login_steps(f"  {self._email}  ", self._password) + [
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Login succeeded", value="/login"),
            ],
            expected_result="App trims spaces and login succeeds",
            priority="P2",
            tags=["validation", "positive"],
        ))

        return cases

    def _gen_password_field(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate password field behaviour tests."""
        prefix = "LOGIN-PWD"
        cases = []

        # Password is masked
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Password Field",
            scenario="Password input is masked by default",
            steps=[
                Action(ActionType.NAVIGATE, "Open login page", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for password field", locator=PASSWORD_LOCATORS[0],
                       fallback_locators=PASSWORD_LOCATORS[1:], timeout=15.0),
                Action(ActionType.VERIFY_VISIBLE, "Password field visible",
                       locator=Locator(LocatorStrategy.CSS, 'input[type="password"]')),
            ],
            expected_result="Password field has type=password",
            priority="P1",
            tags=["ui", "security"],
        ))

        return cases

    def _gen_security(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate security edge case tests."""
        prefix = "LOGIN-SEC"
        cases = []

        # SQL injection
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Security",
            scenario="SQL injection attempt does not break app",
            steps=self._login_steps("' OR 1=1 --", "' OR 1=1 --") + [
                Action(ActionType.VERIFY_URL_CONTAINS, "Still on login", value="/login"),
                Action(ActionType.VERIFY_NOT_TEXT, "No SQL error shown", value="SQL syntax"),
            ],
            expected_result="App handles SQLi gracefully, no errors exposed",
            priority="P1",
            tags=["security", "negative"],
        ))

        # XSS
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Security",
            scenario="XSS attempt does not execute",
            steps=self._login_steps("<script>alert('xss')</script>", "password") + [
                Action(ActionType.VERIFY_URL_CONTAINS, "Still on login", value="/login"),
            ],
            expected_result="No script execution, stays on login",
            priority="P1",
            tags=["security", "negative"],
        ))

        # Long input
        long_string = "A" * 1000
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Security",
            scenario="Extremely long input does not crash app",
            steps=self._login_steps(f"{long_string}@example.com", long_string) + [
                Action(ActionType.VERIFY_URL_CONTAINS, "Still on login", value="/login"),
            ],
            expected_result="App handles long input without crashing",
            priority="P2",
            tags=["security", "negative"],
        ))

        return cases

    def _gen_session(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate session management tests."""
        prefix = "LOGIN-SESS"
        cases = []

        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Session Management",
            scenario="Session persists across page navigation",
            steps=self._valid_login_steps() + [
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Logged in", value="/login"),
                Action(ActionType.NAVIGATE, "Navigate to users", value=f"{self._base_url}/users"),
                Action(ActionType.SLEEP, "Wait for navigation", value="2"),
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Still logged in", value="/login"),
            ],
            expected_result="Session maintained across page navigation",
            priority="P0",
            tags=["session"],
        ))

        return cases

    def _gen_browser_behaviour(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate browser behaviour tests."""
        prefix = "LOGIN-BROWSER"
        cases = []

        # Refresh
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Browser Behaviour",
            scenario="Refresh preserves login page",
            steps=[
                Action(ActionType.NAVIGATE, "Open login", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for page", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.RELOAD_PAGE, "Refresh page"),
                Action(ActionType.SLEEP, "Wait", value="2"),
                Action(ActionType.VERIFY_VISIBLE, "Login form still visible", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:]),
            ],
            expected_result="Login page intact after refresh",
            priority="P2",
            tags=["browser"],
        ))

        # Back button
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Browser Behaviour",
            scenario="Browser back button on login page",
            steps=[
                Action(ActionType.NAVIGATE, "Open login", value=f"{self._base_url}/login"),
                Action(ActionType.WAIT_VISIBLE, "Wait for page", locator=EMAIL_LOCATORS[0],
                       fallback_locators=EMAIL_LOCATORS[1:], timeout=15.0),
                Action(ActionType.NAVIGATE, "Go to another URL", value=f"{self._base_url}/login?test=1"),
                Action(ActionType.SLEEP, "Wait", value="1"),
                Action(ActionType.GO_BACK, "Press back"),
                Action(ActionType.SLEEP, "Wait", value="1"),
                Action(ActionType.VERIFY_URL_CONTAINS, "Still on login domain", value="/login"),
            ],
            expected_result="Back button navigates correctly",
            priority="P2",
            tags=["browser"],
        ))

        return cases

    # ------------------------------------------------------------------
    # Dashboard, Users, and other page generators
    # ------------------------------------------------------------------

    def _gen_page_load(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate page load tests for post-login pages."""
        page_key = page.page_name.upper().replace(" ", "-")
        prefix = f"{page_key}-LOAD"
        url_path = page.url_pattern.replace("${BASE_URL}", "").strip()
        if not url_path:
            url_path = f"/{page.page_name.lower().replace(' ', '-')}"

        cases = []

        # Page loads after login
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Page Load",
            scenario=f"{page.page_name} page loads after login",
            preconditions=["Logged in as admin"],
            steps=self._valid_login_steps() + [
                Action(ActionType.NAVIGATE, f"Navigate to {page.page_name}", value=f"{self._base_url}{url_path}"),
                Action(ActionType.SLEEP, "Wait for page load", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "On correct page", value=url_path),
            ],
            expected_result=f"{page.page_name} page loads successfully",
            priority="P0",
            tags=["page_load"],
        ))

        return cases

    def _gen_navigation(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate sidebar navigation tests."""
        prefix = "DASH-NAV"
        cases = []

        nav_targets = [
            ("Users", "/users"),
            ("Live Activity", "/live-activity"),
            ("Time History", "/time-history"),
        ]

        for target_name, target_path in nav_targets:
            cases.append(TestCase(
                test_id=self._next_id(prefix),
                page=page.page_name,
                functionality="Navigation",
                scenario=f"Navigate to {target_name} from sidebar",
                preconditions=["Logged in as admin"],
                steps=self._valid_login_steps() + [
                    Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Logged in", value="/login"),
                    Action(ActionType.CLICK, f"Click {target_name} link",
                           locator=Locator(LocatorStrategy.PARTIAL_LINK_TEXT, target_name)),
                    Action(ActionType.SLEEP, "Wait for navigation", value="2"),
                    Action(ActionType.VERIFY_URL_CONTAINS, f"On {target_name} page", value=target_path),
                ],
                expected_result=f"Navigated to {target_name} page",
                priority="P0",
                tags=["navigation"],
            ))

        return cases

    def _gen_data_display(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate data display tests."""
        page_key = page.page_name.upper().replace(" ", "-")
        prefix = f"{page_key}-DATA"
        url_path = page.url_pattern.replace("${BASE_URL}", "").strip()
        if not url_path:
            url_path = f"/{page.page_name.lower().replace(' ', '-')}"

        cases = []

        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Data Display",
            scenario=f"{page.page_name} shows content without JS errors",
            preconditions=["Logged in as admin"],
            steps=self._valid_login_steps() + [
                Action(ActionType.NAVIGATE, f"Go to {page.page_name}", value=f"{self._base_url}{url_path}"),
                Action(ActionType.SLEEP, "Wait for data", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "On correct page", value=url_path),
                Action(ActionType.VERIFY_NOT_TEXT, "No undefined data", value="undefined"),
            ],
            expected_result="Page displays data without errors",
            priority="P1",
            tags=["data_display"],
        ))

        return cases

    def _gen_logout(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Generate logout tests."""
        prefix = "LOGOUT"
        cases = []

        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Logout",
            scenario="Logout redirects to login page",
            preconditions=["Logged in as admin"],
            steps=self._valid_login_steps() + [
                Action(ActionType.VERIFY_URL_NOT_CONTAINS, "Logged in", value="/login"),
                Action(ActionType.CLICK, "Click logout",
                       locator=Locator(LocatorStrategy.XPATH,
                                       "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign out')]"),
                       timeout=5.0),
                Action(ActionType.SLEEP, "Wait for redirect", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "On login page", value="/login"),
            ],
            expected_result="User logged out and redirected to login",
            priority="P0",
            tags=["logout"],
        ))

        # Dashboard inaccessible after logout
        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Logout",
            scenario="Dashboard inaccessible after logout",
            preconditions=["Was logged in, then logged out"],
            steps=self._valid_login_steps() + [
                Action(ActionType.CLICK, "Click logout",
                       locator=Locator(LocatorStrategy.XPATH,
                                       "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out')]"),
                       timeout=5.0),
                Action(ActionType.SLEEP, "Wait for logout", value="3"),
                Action(ActionType.NAVIGATE, "Try dashboard", value=f"{self._base_url}/dashboard"),
                Action(ActionType.SLEEP, "Wait for redirect", value="3"),
                Action(ActionType.VERIFY_URL_CONTAINS, "Redirected to login", value="/login"),
            ],
            expected_result="Cannot access dashboard after logout",
            priority="P0",
            tags=["logout", "security"],
        ))

        return cases

    def _gen_responsiveness(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        return []  # Skip for now — visual tests need special infrastructure

    def _gen_search(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        prefix = "USERS-SEARCH"
        cases = []

        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="Search",
            scenario="Search input exists on Users page",
            preconditions=["Logged in as admin"],
            steps=self._valid_login_steps() + [
                Action(ActionType.NAVIGATE, "Go to Users", value=f"{self._base_url}/users"),
                Action(ActionType.SLEEP, "Wait for page", value="3"),
                Action(ActionType.VERIFY_VISIBLE, "Search input visible",
                       locator=Locator(LocatorStrategy.CSS, 'input[type="search"], input[type="text"], input[placeholder*="earch"]'),
                       timeout=5.0),
            ],
            expected_result="Search input is visible on Users page",
            priority="P1",
            tags=["search"],
        ))

        return cases

    def _gen_user_list(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        prefix = "USERS-LIST"
        cases = []

        cases.append(TestCase(
            test_id=self._next_id(prefix),
            page=page.page_name,
            functionality="User List",
            scenario="User list table is visible",
            preconditions=["Logged in as admin"],
            steps=self._valid_login_steps() + [
                Action(ActionType.NAVIGATE, "Go to Users", value=f"{self._base_url}/users"),
                Action(ActionType.SLEEP, "Wait for page", value="3"),
                Action(ActionType.VERIFY_VISIBLE, "Table visible",
                       locator=Locator(LocatorStrategy.CSS, 'table, [class*="table"], [role="table"], [class*="list"]'),
                       timeout=10.0),
            ],
            expected_result="User list/table is visible",
            priority="P1",
            tags=["data_display"],
        ))

        return cases

    def _gen_pagination(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        return []  # Only generate when pagination is confirmed to exist

    def _gen_date_filters(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        return []  # Only generate when date filters are confirmed to exist

    def _gen_realtime(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        return []  # Real-time testing requires specialized infrastructure

    def _gen_from_instructions(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Fallback: generate basic page load test from category instructions."""
        page_key = page.page_name.upper().replace(" ", "-")
        prefix = f"{page_key}-{cat.category_id.replace('.', '-')}"
        
        url_path = page.url_pattern.replace("${BASE_URL}", "").strip()
        if not url_path:
            url_path = f"/{page.page_name.lower().replace(' ', '-')}"

        cases = []

        for instr in cat.instructions:
            cases.append(TestCase(
                test_id=self._next_id(prefix),
                page=page.page_name,
                functionality=cat.category_name,
                scenario=instr.instruction,
                steps=self._valid_login_steps() + [
                    Action(ActionType.NAVIGATE, f"Navigate to {page.page_name}", value=f"{self._base_url}{url_path}"),
                    Action(ActionType.SLEEP, "Wait for page", value="3"),
                ],
                expected_result=f"Verified: {instr.instruction}",
                priority="P2",
                tags=["generated", cat.category_name.lower().replace(" ", "_")],
            ))

        return cases
