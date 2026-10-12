"""
Common Robot Framework variables loaded from environment.

All sensitive values (credentials, URLs) come from environment variables
which are set via .env and passed through Docker Compose.

Usage in .robot files:
    ${BASE_URL}
    ${BROWSER}
    ${SELENIUM_REMOTE_URL}
    ${VALID_USER_EMAIL}
    ${VALID_USER_PASSWORD}
"""

import os


# --- Application Under Test ---
BASE_URL = os.environ.get("BASE_URL", "https://trackpilot.pods365.com")
BROWSER = os.environ.get("BROWSER", "chrome")

# --- Selenium ---
SELENIUM_REMOTE_URL = os.environ.get("SELENIUM_REMOTE_URL", "http://selenium:4444")

# --- Credentials (from .env — NEVER hardcoded) ---
VALID_USER_EMAIL = os.environ.get("VALID_USER_EMAIL", "")
VALID_USER_PASSWORD = os.environ.get("VALID_USER_PASSWORD", "")

# --- Timeouts ---
DEFAULT_TIMEOUT = "15s"
SHORT_TIMEOUT = "5s"
LONG_TIMEOUT = "30s"

# --- Screenshot ---
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/opt/testing/artifacts/screenshots")
