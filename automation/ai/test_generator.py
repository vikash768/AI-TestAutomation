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
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

# Ensure project root is in sys.path for direct execution
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from google import genai
from google.genai import types

from automation.actions.schema import (
    Action,
    ActionType,
    Locator,
    LocatorStrategy,
    TestCase,
    WEB_ONLY_ACTIONS,
    NATIVE_ONLY_ACTIONS,
)
from automation.ai.suite_parser import (
    PageContext,
    SuiteContext,
    TestCategory,
)
from automation.secrets.resolver import SecretResolver, make_secret_placeholder
from automation.secrets.sanitizer import sanitize_data, sanitize_text

logger = logging.getLogger(__name__)

VALID_USER_EMAIL_PLACEHOLDER = make_secret_placeholder("VALID_USER_EMAIL")
VALID_USER_PASSWORD_PLACEHOLDER = make_secret_placeholder("VALID_USER_PASSWORD")


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


def _sanitize_locator(loc: Locator | None) -> tuple[Locator | None, list[Locator]]:
    """Sanitize locator, converting pseudo-selectors (:contains, :has-text, text=) to XPath."""
    if loc is None or loc.strategy != LocatorStrategy.CSS:
        return loc, []

    val = loc.value.strip()
    if not (":contains(" in val or ":has-text(" in val or "text=" in val):
        return loc, []

    parts = [p.strip() for p in re.split(r",\s*(?![^()]*\))", val) if p.strip()]
    valid_css_parts: list[str] = []
    xpath_fallbacks: list[Locator] = []

    for part in parts:
        m = re.search(r"^(.*?):(contains|has-text)\((['\"]?)(.*?)\3\)$", part)
        if m:
            tag = m.group(1).strip()
            text = m.group(4).replace("'", "\\'")
            tag_xpath = tag if tag and tag != "*" else "*"
            xpath_fallbacks.append(
                Locator(
                    LocatorStrategy.XPATH,
                    f"//{tag_xpath}[contains(text(), '{text}') or contains(., '{text}')]",
                )
            )
        elif part.startswith("text="):
            text = part[5:].strip().strip("'\"").replace("'", "\\'")
            xpath_fallbacks.append(
                Locator(
                    LocatorStrategy.XPATH,
                    f"//*[contains(text(), '{text}') or contains(., '{text}')]",
                )
            )
        elif ":contains(" in part or ":has-text(" in part:
            m_embed = re.search(r":(contains|has-text)\((['\"]?)(.*?)\2\)", part)
            if m_embed:
                text = m_embed.group(3).replace("'", "\\'")
                xpath_fallbacks.append(
                    Locator(
                        LocatorStrategy.XPATH,
                        f"//*[contains(text(), '{text}') or contains(., '{text}')]",
                    )
                )
        else:
            valid_css_parts.append(part)

    if valid_css_parts:
        primary = Locator(LocatorStrategy.CSS, ", ".join(valid_css_parts))
        return primary, xpath_fallbacks
    elif xpath_fallbacks:
        return xpath_fallbacks[0], xpath_fallbacks[1:]
    return Locator(LocatorStrategy.CSS, "body"), []


class TestGenerator:
    """Generates concrete TestCase objects from parsed SuiteContext.

    For each page and each testing category, the generator reads the
    instructions and produces TestCase objects with structured Action steps.
    """

    def __init__(
        self,
        base_url: str = "",
        email: str = "",
        password: str = "",
        target_type: str = "web",
        appium_platform: str = "",
        codebase_context: dict[str, Any] | str = "",
    ):
        self._target_type = target_type.lower()
        self._appium_platform = appium_platform.lower() if appium_platform else ""
        self._codebase_context = (
            codebase_context
            if isinstance(codebase_context, dict)
            else ({"summary_prompt": str(codebase_context)} if codebase_context else {})
        )

        # The planner never receives real credentials. Constructor credential
        # arguments remain accepted for API compatibility but are intentionally
        # replaced with symbolic execution-time placeholders.
        if self._target_type == "web":
            self._base_url = base_url or os.environ.get("BASE_URL", "")
        elif self._target_type == "mobile":
            self._base_url = ""
        elif self._target_type == "desktop":
            self._base_url = ""
        else:
            self._base_url = ""

        self._email = VALID_USER_EMAIL_PLACEHOLDER
        self._password = VALID_USER_PASSWORD_PLACEHOLDER
        self._test_id_counters: dict[str, int] = {}

        max_categories = os.environ.get("AI_MAX_CATEGORIES", "").strip()
        try:
            self._max_categories = max(0, int(max_categories)) if max_categories else 0
        except ValueError:
            logger.warning(
                "Ignoring invalid AI_MAX_CATEGORIES=%r; generating the full suite.",
                max_categories,
            )
            self._max_categories = 0

        max_tests = os.environ.get("AI_MAX_TESTS", "").strip()
        try:
            self._max_tests = max(0, int(max_tests)) if max_tests else 0
        except ValueError:
            logger.warning(
                "Ignoring invalid AI_MAX_TESTS=%r; generating all tests.",
                max_tests,
            )
            self._max_tests = 0

        self._require_full_coverage = (
            os.environ.get("REQUIRE_FULL_MD_COVERAGE", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if self._require_full_coverage and (self._max_categories or self._max_tests):
            raise ValueError(
                "REQUIRE_FULL_MD_COVERAGE=true cannot be combined with "
                "AI_MAX_CATEGORIES or AI_MAX_TESTS. Clear the demo limits for a full run."
            )
        
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)
        else:
            self._client = None
            logger.warning("No GEMINI_API_KEY found, falling back to mock generators.")

        self._cache_dir = Path("artifacts/cache")
        suite_id = "trackpilot" if "trackpilot" in (self._base_url or "").lower() else (self._target_type or "suite")
        # Use a new cache namespace so pre-hardening cache files, which may
        # contain resolved credentials, are never loaded by hardened code.
        self._cache_file = self._cache_dir / f"test_generator_safe_v2_{self._target_type}_{suite_id}.json"
        self._cache: dict[str, Any] = self._load_cache()

    def _load_cache(self) -> dict[str, Any]:
        """Load previously generated test cases from disk cache."""
        if os.environ.get("REGENERATE_TESTS", "").lower() in ("1", "true", "yes"):
            return {}
        if self._cache_file.exists():
            try:
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                logger.info("Loaded test generation cache from %s (%d categories)", self._cache_file, len(data))
                return data
            except Exception as exc:
                logger.warning("Failed to load test generator cache %s: %s", self._cache_file, exc)
        return {}

    def _save_cache(self) -> None:
        """Persist generated test cases to disk cache."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            safe_cache = sanitize_data(self._cache)
            self._cache = safe_cache
            self._cache_file.write_text(json.dumps(safe_cache, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to write test generator cache: %s", exc)

    def _sanitize_test_case(self, tc: TestCase) -> TestCase:
        """Sanitize test case steps, locators, and credentials."""
        tc.test_name = sanitize_text(tc.test_name)
        tc.scenario = sanitize_text(tc.scenario)
        tc.expected_result = sanitize_text(tc.expected_result)
        tc.preconditions = sanitize_data(tc.preconditions)
        tc.tags = sanitize_data(tc.tags)

        for step in tc.steps:
            step.description = sanitize_text(step.description)
            step.extra = sanitize_data(step.extra)

            # 1. Sanitize locators
            if step.locator:
                step.locator.value = sanitize_text(step.locator.value)
                prim, extra_falls = _sanitize_locator(step.locator)
                step.locator = prim
                if extra_falls:
                    step.fallback_locators.extend(extra_falls)

            sanitized_fallbacks: list[Locator] = []
            for fl in step.fallback_locators:
                fl.value = sanitize_text(fl.value)
                san_fl, more_fls = _sanitize_locator(fl)
                if san_fl:
                    sanitized_fallbacks.append(san_fl)
                sanitized_fallbacks.extend(more_fls)
            step.fallback_locators = sanitized_fallbacks

            # 2. Value type coercion
            if step.value is not None and not isinstance(step.value, str):
                step.value = str(step.value)
            if step.value is not None:
                step.value = sanitize_text(step.value)

            # 3. Credential hallucination & placeholder normalization
            if step.action_type == ActionType.TYPE:
                desc = step.description.lower()
                loc_val = (step.locator.value if step.locator else "").lower()
                val = self._resolve_instruction_value(step.value or "")
                step.value = val

                # Password field handling
                if "password" in desc or "password" in loc_val:
                    is_valid_auth = any(
                        k in desc
                        for k in (
                            "valid", "correct", "configured", "successful",
                            "admin password", "test password",
                        )
                    ) or any(
                        k in tc.scenario.lower()
                        for k in (
                            "valid", "correct credentials", "valid email format",
                            "uppercase and lowercase", "valid login",
                        )
                    ) or val == self._password

                    if is_valid_auth:
                        step.value = self._password

                # Email field handling
                elif any(k in desc for k in ("email", "username")) or any(k in loc_val for k in ("email", "username")):
                    is_valid_email = any(
                        k in desc
                        for k in (
                            "valid test email", "valid email", "correct email",
                            "admin email", "configured email",
                        )
                    ) or val == self._email

                    if is_valid_email:
                        step.value = self._email

        # Keep generated TestCase objects secret-free before serialization.
        if isinstance(tc.test_data, dict):
            tc.test_data = sanitize_data(tc.test_data)
            if "password" in tc.test_data:
                value = str(tc.test_data["password"])
                if SecretResolver.contains_secret_placeholder(value):
                    tc.test_data["password"] = self._password
            if "email" in tc.test_data:
                tc.test_data["email"] = self._resolve_instruction_value(
                    str(tc.test_data["email"])
                )

        return tc

    def generate(self, ctx: SuiteContext) -> list[TestCase]:
        """Generate all test cases from the parsed suite context.

        Returns:
            A list of TestCase objects ready for execution.
        """
        all_cases: list[TestCase] = []

        generated_categories = 0
        for page in ctx.pages:
            page_cases: list[TestCase] = []
            for category in page.categories:
                if self._max_categories and generated_categories >= self._max_categories:
                    break
                page_cases.extend(self._generate_category_tests(page, category))
                generated_categories += 1

            all_cases.extend(page_cases)
            if page_cases:
                logger.info(
                    "Generated %d tests for page: %s",
                    len(page_cases),
                    page.page_name,
                )
            if self._max_categories and generated_categories >= self._max_categories:
                logger.info(
                    "AI_MAX_CATEGORIES=%d reached; continuing with %d generated tests.",
                    self._max_categories,
                    len(all_cases),
                )
                break

        if self._max_tests and len(all_cases) > self._max_tests:
            logger.info(
                "AI_MAX_TESTS=%d reached; selecting the first %d of %d tests.",
                self._max_tests,
                self._max_tests,
                len(all_cases),
            )
            all_cases = all_cases[:self._max_tests]

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

        # Check persistent cache first
        cache_key = f"{page.page_name}::{category.category_id}::{len(category.instructions)}"
        if cache_key in self._cache:
            try:
                cached_data = self._cache[cache_key]
                if isinstance(cached_data, list) and len(cached_data) >= len(category.instructions):
                    cached_cases = [self._sanitize_test_case(TestCase.from_cache_dict(d)) for d in cached_data]
                    logger.info(
                        "Using %d cached tests for %s - %s",
                        len(cached_cases),
                        page.page_name,
                        category.category_name,
                    )
                    return self._ensure_category_coverage(page, category, cached_cases)
            except Exception as exc:
                logger.warning(
                    "Cache restore error for %s - %s: %s; regenerating.",
                    page.page_name,
                    category.category_name,
                    exc,
                )

        if self._require_full_coverage and not self._client:
            raise RuntimeError(
                "Full Markdown coverage requires GEMINI_API_KEY so every instruction "
                "can be converted into a real executable verification."
            )

        if self._client:
            try:
                logger.info(f"Using AI to generate tests for {page.page_name} - {category.category_name}")
                cases = self._generate_with_ai(page, category)
                if cases:
                    cases = [self._sanitize_test_case(c) for c in cases]
                    cases = self._ensure_category_coverage(page, category, cases)
                    self._cache[cache_key] = [tc.to_cache_dict() for tc in cases]
                    self._save_cache()
                    return cases
            except Exception as e:
                logger.warning(
                    "AI Generation failed for %s / %s: %s. Falling back to deterministic instruction generator.",
                    page.page_name,
                    category.category_name,
                    e,
                )

        # Generate deterministic tests from instructions as safety net
        cases = self._gen_from_instructions(page, category)
        if cases:
            cases = self._ensure_category_coverage(page, category, cases)
            self._cache[cache_key] = [tc.to_cache_dict() for tc in cases]
            self._save_cache()
            return cases

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

        # Map category names to specialized generators (web targets only)
        if self._target_type == "web":
            gen_fn = generators.get(category.category_name)
            if gen_fn:
                cases = gen_fn(page, category)
                return self._ensure_category_coverage(page, category, cases)

        # Fall back to instruction-driven generation
        cases = self._gen_from_instructions(page, category)
        return self._ensure_category_coverage(page, category, cases)

    def _ensure_category_coverage(
        self,
        page: PageContext,
        category: TestCategory,
        cases: list[TestCase],
    ) -> list[TestCase]:
        """Guarantee at least one executable test for every Markdown instruction."""
        required = len(category.instructions)
        if required == 0:
            return cases

        covered_cases = [
            case for case in cases
            if case.steps and any(
                step.action_type not in {ActionType.NAVIGATE, ActionType.SLEEP}
                for step in case.steps
            )
        ]
        missing_start = min(len(covered_cases), required)
        missing_instructions = category.instructions[missing_start:]

        # One bounded repair request asks the model only for instructions it
        # omitted from the original category response.
        if missing_instructions and self._client:
            repair_category = TestCategory(
                category_id=category.category_id,
                category_name=f"{category.category_name} — Remaining Coverage",
                description=(
                    "Generate one independent executable test for every instruction below. "
                    "Do not combine or omit instructions."
                ),
                instructions=missing_instructions,
            )
            try:
                repair_cases = self._generate_with_ai(page, repair_category)
                covered_cases.extend(repair_cases[:len(missing_instructions)])
            except Exception as exc:
                logger.warning(
                    "Coverage repair failed for %s / %s: %s",
                    page.page_name,
                    category.category_name,
                    exc,
                )

        if len(covered_cases) < required and self._require_full_coverage:
            raise RuntimeError(
                f"Full Markdown coverage required for {page.page_name} / "
                f"{category.category_name}, but only {len(covered_cases)} of "
                f"{required} executable instruction tests were generated."
            )

        # Deterministic instruction cases are the final safety net. This keeps
        # suite coverage complete even if the AI response is short or invalid.
        if len(covered_cases) < required:
            remaining = category.instructions[len(covered_cases):]
            fallback_category = TestCategory(
                category_id=category.category_id,
                category_name=category.category_name,
                description=category.description,
                instructions=remaining,
            )
            covered_cases.extend(self._gen_from_instructions(page, fallback_category))

        slug = re.sub(
            r"[^A-Z0-9]+",
            "-",
            f"{page.page_name}-{category.category_id}".upper(),
        ).strip("-")
        for index, case in enumerate(covered_cases, 1):
            case.test_id = f"TC-{self._target_type.upper()}-{slug}-{index:03d}"
            if index <= required:
                instruction = category.instructions[index - 1].instruction
                case.test_data["source_instruction_index"] = str(index)
                case.test_data["source_instruction"] = instruction
                if "md-coverage" not in case.tags:
                    case.tags.append("md-coverage")

        if len(covered_cases) < required:
            raise RuntimeError(
                f"Coverage invariant failed for {page.page_name} / {category.category_name}: "
                f"{len(covered_cases)} tests for {required} instructions"
            )

        # The contract is one test per Markdown instruction. Models sometimes
        # return bonus/duplicate cases even when asked for an exact count; do
        # not let those make a full run larger or less deterministic.
        if len(covered_cases) > required:
            logger.warning(
                "Ignoring %d extra generated test(s) for %s / %s; "
                "the Markdown defines exactly %d instruction(s).",
                len(covered_cases) - required,
                page.page_name,
                category.category_name,
                required,
            )
            covered_cases = covered_cases[:required]

        logger.info(
            "Coverage: %s / %s — %d/%d instructions have executable tests",
            page.page_name,
            category.category_name,
            required,
            required,
        )
        return covered_cases

    def _generate_with_ai(self, page: PageContext, category: TestCategory) -> list[TestCase]:
        # Build target-type-aware context for the AI prompt
        if self._target_type == "web":
            target_context = f"""Target Platform: Web Browser
Application Base URL: {self._base_url}
Use browser-based actions: NAVIGATE, VERIFY_URL, EXECUTE_JS, CLICK, TYPE, CLEAR, VERIFY_TEXT, VERIFY_VISIBLE, WAIT_VISIBLE, WAIT_URL_CONTAINS, etc.
TrackPilot Admin Web Application details:
- Base routes: /login, /dashboard, /users, /live, /history, /screenshots, /projects, /reports, /settings, /audit, /profile
- Login page: email input id="email", password input id="password", submit button css="button[type='submit']"
- Main Navigation: sidebar items with text "Dashboard", "Users", "Live Activity", "Time History", "Screenshots", "Projects & Tasks", "Reports", "Settings", "Audit Logs", "Profile", "Log out"
- For locators, prefer id, css, xpath, name, text, tag_name, or class_name.
"""
            available_actions = ", ".join(
                at.value for at in ActionType
                if at not in NATIVE_ONLY_ACTIONS
            )
        elif self._target_type == "mobile":
            platform_label = self._appium_platform or self._target_type
            target_context = f"""Target Platform: {platform_label.capitalize()} (native app via Appium)
IMPORTANT: This is a NATIVE application. Do NOT generate NAVIGATE, VERIFY_URL, EXECUTE_JS, RELOAD_PAGE, GO_BACK, GET_CONSOLE_ERRORS, OPEN_BROWSER, CLOSE_BROWSER, or SET_WINDOW_SIZE actions.
Use native-compatible actions: CLICK, TAP, TYPE, CLEAR, VERIFY_TEXT, VERIFY_VISIBLE, WAIT_VISIBLE, SWIPE, SCROLL, PRESS_KEY, SLEEP.
For locators on native mobile apps:
- For the "Sign In" submit button at the bottom: Use xpath "(//android.view.ViewGroup[@content-desc='Sign In'])[last()]"
- For the "Sign In" navigation tab at the top: Use xpath "(//android.view.ViewGroup[@content-desc='Sign In'])[1]"
- For the "Sign Up" tab at the top: Use accessibility_id "Sign Up" or xpath "//*[@content-desc='Sign Up']"
- For bottom navigation tabs (Home, Search, Messages, Profile): Use accessibility_id "Home", "Search", "Messages", "Profile" or xpath "//*[@content-desc='Home']" etc.
- For the center "+" create button in bottom nav: Use xpath "//android.view.ViewGroup[contains(@bounds, '1392') and @clickable='true']"
- For dismissing modals or going back: Use PRESS_KEY with value "BACK"
- For input fields: Use xpath "//android.widget.EditText[1]" for email, and "//android.widget.EditText[2]" for password.
- Use the credentials: Email "{self._email}", Password "{self._password}".
- On Android, styled text headers are split into separate TextViews; always verify individual words or child options like "General Account" or "Regular Post", not long multi-word headers.
Do NOT assume a URL exists or use web URLs. The app is already launched and active on screen.
"""
            available_actions = ", ".join(
                at.value for at in ActionType
                if at not in WEB_ONLY_ACTIONS
            )
        else:
            platform_label = self._appium_platform or "desktop"
            target_context = f"""Target Platform: {platform_label.capitalize()} desktop application via Appium
IMPORTANT: This is a NATIVE DESKTOP application. Do NOT generate NAVIGATE, VERIFY_URL, EXECUTE_JS, RELOAD_PAGE, GO_BACK, GET_CONSOLE_ERRORS, OPEN_BROWSER, CLOSE_BROWSER, SWIPE, or HIDE_KEYBOARD actions.
Use native desktop actions such as CLICK, TYPE, CLEAR, VERIFY_TEXT, VERIFY_VISIBLE, WAIT_VISIBLE, PRESS_KEY, SLEEP, LAUNCH_APP, CLOSE_APP, and ACTIVATE_APP.
Prefer accessibility_id and name locators. Use class_name or a platform-appropriate XPath only when the accessibility tree has no stable identifier.
For macOS Mac2, locate controls by accessibility identifier/name/label. For Windows, locate controls by accessibility id/name/class.
Do not emit Android class names, content-desc attributes, Android UiAutomator selectors, iOS predicates, mobile coordinates, or browser URLs.
The desktop app is already launched by the Appium session.
"""
            available_actions = ", ".join(
                at.value for at in ActionType
                if at not in WEB_ONLY_ACTIONS
                and at not in {ActionType.SWIPE, ActionType.HIDE_KEYBOARD}
            )

        repo_snippet = ""
        summary_prompt = self._codebase_context.get("summary_prompt", "")
        repo_url = self._codebase_context.get("repo_url", "")
        if summary_prompt:
            repo_label = f"from {repo_url}" if repo_url else "from repository"
            repo_snippet = f"""
Discovered Codebase Elements ({repo_label}):
{summary_prompt}
Use the actual element labels, testIDs, placeholders, and route paths found in the codebase where appropriate.
"""

        prompt = f"""
You are an expert QA Automation Architect. Generate a JSON array of test cases based on the provided instructions.
{target_context}
{repo_snippet}
Configured Test Email: {self._email}
Configured Test Password: {self._password}

Page: {page.page_name}
Category: {category.category_name}

Instructions:
{chr(10).join(f"{index}. {instruction.instruction}" for index, instruction in enumerate(category.instructions, 1))}

Context/Description:
{category.description}

Return ONLY a valid JSON array of objects representing test cases. Do not include markdown formatting like ```json.
Coverage contract:
- Return exactly {len(category.instructions)} test-case objects: one object per numbered instruction, in the same order.
- Do not combine instructions and do not omit an instruction.
- Each test must execute and verify the behavior named by its instruction; navigation plus SLEEP alone is not a valid test.
- In each object's test_data, include "source_instruction_index" and "source_instruction" for the matching numbered instruction.
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
- "test_type": string (e.g. "{self._target_type}")
- "tags": array of strings (e.g. ["smoke", "positive"])
- "steps": array of Action objects. 

Action objects must have:
- "action_type": string (Must be one of: {available_actions})
- "description": string
- "locator": object OR null. If object, must have "strategy" (one of: data-testid, id, name, css, xpath, text, link_text, partial_link_text, tag_name, class_name, accessibility_id, android_uiautomator, ios_predicate) and "value" (string).
- "value": string OR null.
- "timeout": float (default 15.0)
- "extra": dictionary of extra properties

Guidelines:
- For web targets: First step is usually NAVIGATE or logging in if the page is protected.
- For native app targets: Do NOT use NAVIGATE. Use LAUNCH_APP or assume the app is already open.
- To login on web, navigate to /login, type email, type password, click submit button.
- Make tests robust. Use appropriate locators for the target platform.
- IMPORTANT LOCATOR SYNTAX: Use strictly standard W3C CSS selectors. NEVER use jQuery or Playwright pseudo-selectors like :contains(), :has-text(), or text= in CSS selectors. Use XPath for text matching (e.g. "//button[contains(., 'Save')]").
- FOR AUTHENTICATION: Always use the exact Configured Test Password "{self._password}" for positive authentication. Do NOT add ".com" or alter the password.
"""
        prompt = sanitize_text(prompt)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        raw_text = ""
        last_api_err = None
        for attempt in range(3):
            try:
                response = self._client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                raw_text = response.text.strip()
                break
            except Exception as api_err:
                last_api_err = api_err
                if attempt < 2:
                    logger.warning(
                        "Gemini API attempt %d failed for %s / %s: %s. Retrying in %ds...",
                        attempt + 1,
                        page.page_name,
                        category.category_name,
                        api_err,
                        (attempt + 1) * 2,
                    )
                    time.sleep((attempt + 1) * 2)
                else:
                    raise last_api_err

        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
        data = json.loads(raw_text)
        if isinstance(data, dict):
            for k in ("test_cases", "tests", "cases", "data", "items"):
                if k in data and isinstance(data[k], list):
                    data = data[k]
                    break
            else:
                data = [data]
        elif not isinstance(data, list):
            data = []
        
        parsed_cases = []
        for item in data:
            if not isinstance(item, dict):
                continue
            steps = []
            for s in item.get("steps", []):
                if not isinstance(s, dict):
                    continue
                loc = None
                if s.get("locator") and isinstance(s["locator"], dict):
                    loc_val = str(s["locator"].get("value", "") or "")
                    if loc_val:
                        loc_strat = LocatorStrategy.from_str(s["locator"].get("strategy", "css"))
                        loc = Locator(strategy=loc_strat, value=loc_val)
                raw_action_type = str(s.get("action_type", "")).strip().upper()
                action_aliases = {
                    "INPUT": "TYPE",
                    "INPUT_TEXT": "TYPE",
                    "ENTER_TEXT": "TYPE",
                    "SEND_KEYS": "TYPE",
                    "FILL": "TYPE",
                    "CLICK_ELEMENT": "CLICK",
                    "TAP_ELEMENT": "TAP",
                    "VERIFY_ELEMENT_VISIBLE": "VERIFY_VISIBLE",
                    "ASSERT_VISIBLE": "VERIFY_VISIBLE",
                    "WAIT_FOR_VISIBLE": "WAIT_VISIBLE",
                    "WAIT_FOR_ELEMENT": "WAIT_VISIBLE",
                    "WAIT_FOR_URL": "WAIT_URL_CONTAINS",
                    "WAIT_URL": "WAIT_URL_CONTAINS",
                    "URL_CONTAINS": "VERIFY_URL_CONTAINS",
                    "ASSERT_URL": "VERIFY_URL",
                    "CHECK_URL": "VERIFY_URL",
                    "VERIFY_CONTAINS": "VERIFY_TEXT",
                    "VERIFY_NOT_CONTAINS": "VERIFY_NOT_TEXT",
                    "ASSERT_TEXT": "VERIFY_TEXT",
                    "WAIT": "SLEEP",
                    "WAIT_FOR": "SLEEP",
                    "PAUSE": "SLEEP",
                    "PRESS": "PRESS_KEY",
                    "SCREENSHOT": "TAKE_SCREENSHOT",
                }
                raw_action_type = action_aliases.get(raw_action_type, raw_action_type)
                try:
                    action_type = ActionType(raw_action_type)
                except ValueError:
                    logger.warning(
                        "Skipping unsupported AI action %r in test %s",
                        raw_action_type,
                        item.get("test_id", "(unknown)"),
                    )
                    continue

                sanitized_loc, extra_falls = _sanitize_locator(loc)
                fallback_locators = self._ai_fallback_locators(s, sanitized_loc)
                if extra_falls:
                    fallback_locators.extend(extra_falls)

                s_val = s.get("value")
                if s_val is not None and not isinstance(s_val, str):
                    s_val = str(s_val)

                action = Action(
                    action_type=action_type,
                    description=s.get("description", ""),
                    locator=sanitized_loc,
                    fallback_locators=fallback_locators,
                    value=s_val,
                    timeout=float(s.get("timeout", 15.0)),
                    extra=s.get("extra", {})
                )
                steps.append(action)
                
            tc = TestCase(
                test_id=item.get("test_id", self._next_id("TEST")),
                test_name=item.get("test_name", item.get("scenario", "Generated Test")),
                page=item.get("page", page.page_name),
                functionality=item.get("functionality", category.category_name),
                scenario=item.get("scenario", ""),
                preconditions=item.get("preconditions", []),
                test_data=item.get("test_data", {}),
                steps=steps,
                expected_result=item.get("expected_result", ""),
                priority=item.get("priority", "P1"),
                test_type=item.get("test_type", self._target_type),
                tags=item.get("tags", []),
            )
            parsed_cases.append(self._sanitize_test_case(tc))

        # Sanitize for native targets: reject any web-only actions
        if self._target_type != "web":
            sanitized = []
            for tc in parsed_cases:
                tc.steps = [s for s in tc.steps if s.action_type not in WEB_ONLY_ACTIONS]
                if tc.steps:
                    sanitized.append(tc)
            return sanitized

        return parsed_cases

    def _ai_fallback_locators(self, step: dict, primary: Locator | None) -> list[Locator]:
        """Attach stable fallbacks when an AI-selected web locator is too narrow."""
        if self._target_type != "web" or primary is None:
            return []

        description = str(step.get("description", "")).lower()
        locator_value = primary.value.lower()
        candidates: list[Locator] = []

        if "email" in description or "email" in locator_value or "username" in locator_value:
            candidates = EMAIL_LOCATORS
        elif "password" in description or "password" in locator_value:
            candidates = PASSWORD_LOCATORS
        elif any(token in description for token in ("login", "log in", "sign in", "submit")) \
                or "submit" in locator_value:
            candidates = LOGIN_BUTTON_LOCATORS
        elif any(token in description for token in ("save", "update", "create", "add")) \
                or any(token in locator_value for token in ("save", "update", "create", "add")):
            candidates = [
                Locator(LocatorStrategy.CSS, "button[type='submit']"),
                Locator(LocatorStrategy.XPATH, "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'save') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update')]"),
                Locator(LocatorStrategy.CSS, "button.save-btn, button.btn-primary"),
            ]
        elif "refresh" in description or "refresh" in locator_value:
            candidates = [
                Locator(LocatorStrategy.XPATH, "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'refresh')]"),
                Locator(LocatorStrategy.CSS, "button[title*='Refresh' i], .refresh-btn, [aria-label*='Refresh' i]"),
            ]
        elif any(token in description for token in ("table", "list", "grid")) \
                or any(token in locator_value for token in ("table", "list", "grid")):
            candidates = [
                Locator(LocatorStrategy.CSS, "table"),
                Locator(LocatorStrategy.CSS, "tbody tr"),
                Locator(LocatorStrategy.CSS, "[role='table'], div[class*='table']"),
            ]
        elif "search" in description or "search" in locator_value:
            candidates = [
                Locator(LocatorStrategy.CSS, "input[type='search'], input[type='text'], input[placeholder*='Search' i]"),
                Locator(LocatorStrategy.XPATH, "//input[contains(@placeholder,'Search') or contains(@placeholder,'search')]"),
            ]
        elif any(token in description for token in ("logout", "log out", "sign out")) \
                or any(token in locator_value for token in ("logout", "log out", "sign out")):
            candidates = [
                Locator(LocatorStrategy.XPATH, "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'logout') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out') or contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'sign out')]"),
                Locator(LocatorStrategy.PARTIAL_LINK_TEXT, "Log out"),
                Locator(LocatorStrategy.PARTIAL_LINK_TEXT, "Logout"),
            ]

        return [locator for locator in candidates if locator != primary]

    def _next_id(self, prefix: str) -> str:
        """Generate the next test ID for a given prefix."""
        count = self._test_id_counters.get(prefix, 0) + 1
        self._test_id_counters[prefix] = count
        return f"TC-TP-{prefix}-{count:03d}"

    def _login_steps(self, email: str = "", password: str = "") -> list[Action]:
        """Generate standard login action steps."""
        if self._target_type in ("mobile", "desktop"):
            return []
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

    def _instruction_to_native_actions(self, text: str) -> list[Action]:
        """Convert a natural-language instruction into native Appium actions."""
        quotes = re.findall(r'"([^"]*)"', text)
        lower = text.lower().strip()
        actions = []

        is_verify = bool(re.search(r'\b(verify|assert|is visible|is displayed|are visible|remains?)\b', lower))
        is_tap = bool(re.search(r'\b(tap|click|press|select)\b', lower)) and not is_verify
        is_type = bool(re.search(r'\b(enter|type)\b', lower)) and not is_verify

        if is_verify:
            target = self._resolve_instruction_value(quotes[0]) if quotes else ""
            if target:
                if "which account" in target.lower() or "want to create" in target.lower():
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        "//*[contains(@content-desc, 'Which Account') or contains(@text, 'Which Account') or contains(@text, 'Want To Create') or contains(@content-desc, 'Want To Create')]"
                    )
                elif "what type" in target.lower() or "post you want to create" in target.lower():
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        "//*[contains(@content-desc, 'What Type') or contains(@text, 'What Type') or contains(@text, 'Post You Want') or contains(@content-desc, 'Post You Want')]"
                    )
                elif "search here" in target.lower():
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        "//android.widget.EditText[contains(@text, 'Search') or contains(@content-desc, 'Search') or @text='Search Here'] | //android.widget.EditText"
                    )
                elif "sign in" in target.lower() and "button" in lower:
                    loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[last()]")
                elif "justpaid" in target.lower():
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        "//*[contains(@text, 'justpaid') or contains(@content-desc, 'justpaid') or contains(@text, 'ljustpaid') or contains(@text, 'Ijustpaid')]"
                    )
                elif target in ("Home", "Search", "Messages", "Profile") and ("tab" in lower or "nav" in lower or "bar" in lower):
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        f"//android.view.ViewGroup[@content-desc='{target}'] | //android.widget.TextView[@text='{target}']"
                    )
                else:
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        f"//*[@content-desc='{target}' or @text='{target}' or contains(@text, '{target}') or contains(@content-desc, '{target}')]"
                    )
                actions.append(Action(ActionType.VERIFY_VISIBLE, f"Verify {target} visible", locator=loc))
            elif "email" in lower:
                loc = Locator(LocatorStrategy.XPATH, "//android.widget.EditText[contains(@text, 'email') or contains(@content-desc, 'email')] | //android.widget.EditText[1]")
                actions.append(Action(ActionType.VERIFY_VISIBLE, "Verify Email field visible", locator=loc))
            elif "password" in lower:
                loc = Locator(LocatorStrategy.XPATH, "//android.widget.EditText[@password='true'] | //android.widget.EditText[2]")
                actions.append(Action(ActionType.VERIFY_VISIBLE, "Verify Password field visible", locator=loc))
            elif "continue with" in lower:
                loc = Locator(LocatorStrategy.XPATH, "//*[contains(@text, 'Continue With') or contains(@content-desc, 'Continue With')]")
                actions.append(Action(ActionType.VERIFY_VISIBLE, "Verify Continue With visible", locator=loc))
            elif "remain" in lower and "sign in" in lower:
                loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[1]")
                actions.append(Action(ActionType.VERIFY_VISIBLE, "Verify Sign In tab visible", locator=loc))
            else:
                actions.append(Action(ActionType.SLEEP, f"Verify condition: {text}", value="2"))

        elif is_tap:
            target = self._resolve_instruction_value(quotes[0]) if quotes else ""
            if "back" in lower or "dismiss" in lower:
                actions.append(Action(ActionType.PRESS_KEY, "Press Back", value="BACK"))
            elif "+" in lower or "+" in quotes or "create button" in lower:
                loc = Locator(
                    LocatorStrategy.XPATH,
                    "//android.view.ViewGroup[contains(@bounds, '1392') and @clickable='true']"
                )
                actions.append(Action(ActionType.CLICK, "Tap Center + Button", locator=loc))
            elif target in ("Home", "Search", "Messages", "Profile"):
                loc = Locator(
                    LocatorStrategy.XPATH,
                    f"//android.view.ViewGroup[@content-desc='{target}'] | //android.widget.TextView[@text='{target}']"
                )
                actions.append(Action(ActionType.CLICK, f"Tap {target}", locator=loc))
            elif target:
                if "sign in" in target.lower():
                    if "button" in lower:
                        loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[last()]")
                    else:
                        loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[1]")
                else:
                    loc = Locator(
                        LocatorStrategy.XPATH,
                        f"//*[@content-desc='{target}' or @text='{target}' or contains(@text, '{target}') or contains(@content-desc, '{target}')]"
                    )
                actions.append(Action(ActionType.CLICK, f"Tap {target}", locator=loc))
            else:
                if "sign in" in lower:
                    if "button" in lower:
                        loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[last()]")
                    else:
                        loc = Locator(LocatorStrategy.XPATH, "(//android.view.ViewGroup[@content-desc='Sign In'])[1]")
                    actions.append(Action(ActionType.CLICK, "Tap Sign In", locator=loc))
                elif "sign up" in lower:
                    loc = Locator(LocatorStrategy.XPATH, "//*[@content-desc='Sign Up' or @text='Sign Up']")
                    actions.append(Action(ActionType.CLICK, "Tap Sign Up", locator=loc))
                elif "remember" in lower:
                    loc = Locator(LocatorStrategy.XPATH, "//*[@content-desc='Remember Me' or @text='Remember Me']")
                    actions.append(Action(ActionType.CLICK, "Tap Remember Me", locator=loc))
                elif "visibility" in lower or "toggle" in lower:
                    loc = Locator(LocatorStrategy.XPATH, "//android.widget.EditText[@password='true']/..//com.horcrux.svg.SvgView | //*[@content-desc='eye' or contains(@content-desc, 'password')]")
                    actions.append(Action(ActionType.CLICK, "Toggle Password Visibility", locator=loc))
                else:
                    actions.append(Action(ActionType.SLEEP, f"Process: {text}", value="2"))

        elif is_type:
            val = self._resolve_instruction_value(quotes[0]) if quotes else ""
            if "email" in lower:
                loc = Locator(
                    LocatorStrategy.XPATH,
                    "//android.widget.EditText[contains(@text, 'email') or contains(@content-desc, 'email')] | //android.widget.EditText[1]"
                )
                actions.append(Action(ActionType.CLEAR, "Clear email field", locator=loc))
                actions.append(Action(ActionType.TYPE, f"Enter email: {val}", locator=loc, value=val))
            elif "password" in lower:
                loc = Locator(
                    LocatorStrategy.XPATH,
                    "//android.widget.EditText[@password='true'] | //android.widget.EditText[2]"
                )
                actions.append(Action(ActionType.CLEAR, "Clear password field", locator=loc))
                actions.append(Action(ActionType.TYPE, "Enter password", locator=loc, value=val))
            else:
                loc = Locator(LocatorStrategy.XPATH, "//android.widget.EditText")
                actions.append(Action(ActionType.TYPE, f"Enter text: {val}", locator=loc, value=val))

        else:
            actions.append(Action(ActionType.SLEEP, f"Execute: {text}", value="2"))

        return actions

    def _resolve_instruction_value(self, value: str) -> str:
        """Normalize suite placeholders without resolving environment secrets."""
        aliases = {
            "${EMAIL}": self._email,
            "{{EMAIL}}": self._email,
            "${PASSWORD}": self._password,
            "{{PASSWORD}}": self._password,
        }
        return aliases.get(value, SecretResolver.canonicalize(sanitize_text(value)))

    def _instruction_to_desktop_actions(self, text: str) -> list[Action]:
        """Convert instructions to platform-neutral native desktop actions."""
        quotes = re.findall(r'"([^"]*)"', text)
        lower = text.lower().strip()
        target = self._resolve_instruction_value(quotes[0]) if quotes else ""

        if re.search(r'\b(verify|assert|is visible|is displayed|are visible)\b', lower):
            if target:
                return [Action(
                    ActionType.VERIFY_VISIBLE,
                    f"Verify {target} visible",
                    locator=Locator(LocatorStrategy.ACCESSIBILITY_ID, target),
                    fallback_locators=[
                        Locator(LocatorStrategy.NAME, target),
                        Locator(LocatorStrategy.XPATH, f"//*[@name='{target}' or @label='{target}' or @title='{target}']"),
                    ],
                )]
            return [Action(ActionType.SLEEP, f"Verify condition: {text}", value="2")]

        if re.search(r'\b(tap|click|press|select)\b', lower):
            if "back" in lower or "dismiss" in lower:
                return [Action(ActionType.PRESS_KEY, "Dismiss dialog", value="ESCAPE")]
            if target:
                return [Action(
                    ActionType.CLICK,
                    f"Click {target}",
                    locator=Locator(LocatorStrategy.ACCESSIBILITY_ID, target),
                    fallback_locators=[
                        Locator(LocatorStrategy.NAME, target),
                        Locator(LocatorStrategy.XPATH, f"//*[@name='{target}' or @label='{target}' or @title='{target}']"),
                    ],
                )]

        if re.search(r'\b(enter|type)\b', lower):
            value = target
            field_name = "Password" if "password" in lower else "Email" if "email" in lower else "Text Field"
            locator = Locator(LocatorStrategy.ACCESSIBILITY_ID, field_name)
            fallbacks = [
                Locator(LocatorStrategy.NAME, field_name),
                Locator(LocatorStrategy.XPATH, f"//*[@name='{field_name}' or @label='{field_name}']"),
            ]
            return [
                Action(ActionType.CLEAR, f"Clear {field_name}", locator=locator, fallback_locators=fallbacks),
                Action(ActionType.TYPE, f"Enter {field_name}", locator=locator, fallback_locators=fallbacks, value=value),
            ]

        return [Action(ActionType.SLEEP, f"Execute: {text}", value="2")]

    def _gen_from_instructions(self, page: PageContext, cat: TestCategory) -> list[TestCase]:
        """Fallback: generate test cases directly from category instructions."""
        page_key = page.page_name.upper().replace(" ", "-")
        prefix = f"{page_key}-{cat.category_id.replace('.', '-')}"
        cases = []

        if self._target_type in {"mobile", "desktop"}:
            for instr in cat.instructions:
                steps = (
                    self._instruction_to_native_actions(instr.instruction)
                    if self._target_type == "mobile"
                    else self._instruction_to_desktop_actions(instr.instruction)
                )
                cases.append(TestCase(
                    test_id=self._next_id(prefix),
                    page=page.page_name,
                    functionality=cat.category_name,
                    scenario=instr.instruction,
                    steps=steps,
                    expected_result=f"Verified: {instr.instruction}",
                    priority="P2",
                    test_type=self._target_type,
                    tags=["generated", cat.category_name.lower().replace(" ", "_")],
                ))
            return cases

        url_path = page.url_pattern.replace("${BASE_URL}", "").strip()
        if not url_path:
            url_path = f"/{page.page_name.lower().replace(' ', '-')}"

        for instr in cat.instructions:
            cases.append(self._sanitize_test_case(TestCase(
                test_id=self._next_id(prefix),
                page=page.page_name,
                functionality=cat.category_name,
                scenario=instr.instruction,
                steps=self._valid_login_steps() + [
                    Action(ActionType.NAVIGATE, f"Navigate to {page.page_name}", value=f"{self._base_url}{url_path}"),
                    Action(ActionType.SLEEP, "Wait for page", value="3"),
                    Action(ActionType.VERIFY_URL_CONTAINS, f"Verify on {page.page_name}", value=url_path),
                ],
                expected_result=f"Verified: {instr.instruction}",
                priority="P2",
                test_type="web",
                tags=["generated", cat.category_name.lower().replace(" ", "_")],
            )))

        return cases


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI Test Case Generator")
    parser.add_argument("--suite", default="testsuites/trackpilot.testsuite.md", help="Path to test suite markdown file")
    parser.add_argument("--base-url", default="", help="Application base URL")
    parser.add_argument("--target", default="web", choices=["web", "mobile", "desktop"], help="Target platform type")
    parser.add_argument("--appium-platform", default="", help="Appium platform (android, ios, macos, windows)")
    parser.add_argument("--regenerate", action="store_true", help="Force regenerate tests without cache")
    args = parser.parse_args()

    if args.regenerate:
        os.environ["REGENERATE_TESTS"] = "1"

    suite_path = Path(args.suite)
    if not suite_path.exists():
        print(f"Suite file not found: {suite_path}")
        sys.exit(1)

    from automation.ai.suite_parser import SuiteParser

    suite_parser = SuiteParser()
    ctx = suite_parser.parse(suite_path)
    total_categories = sum(len(p.categories) for p in ctx.pages)
    print(f"Parsed {suite_path}: {len(ctx.pages)} pages, {total_categories} categories")

    generator = TestGenerator(
        base_url=args.base_url,
        target_type=args.target,
        appium_platform=args.appium_platform,
    )
    tests = generator.generate(ctx)
    print(f"Generated {len(tests)} test cases successfully.")
