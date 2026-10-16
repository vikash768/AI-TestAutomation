"""
Common Robot Framework configuration and symbolic credential variables.

Non-secret configuration comes from environment variables passed through
Docker Compose. Credentials remain symbolic here and are resolved only by
the secure execution keyword immediately before Selenium input.

Usage in .robot files:
    ${BASE_URL}
    ${BROWSER}
    ${SELENIUM_REMOTE_URL}
    ${VALID_USER_EMAIL}
    ${VALID_USER_PASSWORD}
"""

import os

from automation.secrets.resolver import make_secret_placeholder


# --- Application Under Test ---
BASE_URL = os.environ.get("BASE_URL", "")
BROWSER = os.environ.get("BROWSER", "chrome")

# --- Selenium ---
SELENIUM_REMOTE_URL = os.environ.get("SELENIUM_REMOTE_URL", "http://selenium:4444")

# --- Credentials (resolved only by automation.robot.secure_keywords) ---
VALID_USER_EMAIL = make_secret_placeholder("VALID_USER_EMAIL")
VALID_USER_PASSWORD = make_secret_placeholder("VALID_USER_PASSWORD")

# --- Timeouts ---
DEFAULT_TIMEOUT = "15s"
SHORT_TIMEOUT = "5s"
LONG_TIMEOUT = "30s"

# --- Screenshot ---
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/opt/testing/artifacts/screenshots")
