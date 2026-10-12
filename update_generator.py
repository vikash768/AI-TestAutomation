import re
import os

filepath = "/Users/vikashmandal/Desktop/AI-TestAutomation/automation/ai/test_generator.py"
with open(filepath, "r") as f:
    content = f.read()

# Add imports for json and google.generativeai
if "import json" not in content:
    content = content.replace("import logging", "import json\nimport logging\nimport google.generativeai as genai")

# Update TestGenerator.__init__
init_replacement = """    def __init__(self, base_url: str = "", email: str = "", password: str = ""):
        self._base_url = base_url or os.environ.get("BASE_URL", "https://trackpilot.pods365.com")
        self._email = email or os.environ.get("VALID_USER_EMAIL", "")
        self._password = password or os.environ.get("VALID_USER_PASSWORD", "")
        self._test_id_counters: dict[str, int] = {}
        
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self._model = genai.GenerativeModel('gemini-1.5-pro')
        else:
            self._model = None
            logger.warning("No GEMINI_API_KEY found, falling back to mock generators.")"""

content = re.sub(r'    def __init__\(self, base_url: str.*?self\._test_id_counters: dict\[str, int\] = \{\}', init_replacement, content, flags=re.DOTALL)

# Update _generate_category_tests
gen_cat_replacement = """    def _generate_category_tests(
        self, page: PageContext, category: TestCategory
    ) -> list[TestCase]:
        \"\"\"Generate tests for a single category within a page.\"\"\"
        cases = []

        if self._model:
            try:
                logger.info(f"Using AI to generate tests for {page.page_name} - {category.category_name}")
                cases = self._generate_with_ai(page, category)
                if cases:
                    return cases
            except Exception as e:
                logger.error(f"AI Generation failed: {e}")

        # Map category names to specialized generators
        generators = {"""

content = re.sub(r'    def _generate_category_tests\(\s*self, page: PageContext, category: TestCategory\s*\) -> list\[TestCase\]:.*?generators = \{', gen_cat_replacement, content, flags=re.DOTALL)

ai_gen_method = """
    def _generate_with_ai(self, page: PageContext, category: TestCategory) -> list[TestCase]:
        prompt = f\"\"\"
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
\"\"\"
        response = self._model.generate_content(prompt, generation_config=genai.GenerationConfig(response_mime_type="application/json"))
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
"""

# Insert _generate_with_ai before _next_id
content = content.replace("    def _next_id(self, prefix: str) -> str:", ai_gen_method + "\n    def _next_id(self, prefix: str) -> str:")

# Fix fallback generators missing test_name by passing test_name=scenario
content = re.sub(r'(TestCase\(\s*test_id=.*?,\s*page=.*?,\s*functionality=.*?,\s*scenario=.*?)(,\s*preconditions)', r'\1, test_name=scenario\2', content, flags=re.DOTALL)
content = re.sub(r'(TestCase\(\s*test_id=.*?,\s*page=.*?,\s*functionality=.*?,\s*scenario=.*?)(,\s*test_data)', r'\1, test_name=scenario\2', content, flags=re.DOTALL)
content = re.sub(r'(TestCase\(\s*test_id=.*?,\s*page=.*?,\s*functionality=.*?,\s*scenario=.*?)(,\s*steps)', r'\1, test_name=scenario\2', content, flags=re.DOTALL)

with open(filepath, "w") as f:
    f.write(content)
