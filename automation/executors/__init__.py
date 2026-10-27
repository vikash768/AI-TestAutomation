"""Executors package — test execution across Selenium and Appium."""

from automation.executors.appium_executor import AppiumExecutor
from automation.executors.selenium_executor import (
    ActionExecutionError,
    SeleniumExecutor,
)

__all__ = [
    "SeleniumExecutor",
    "AppiumExecutor",
    "ActionExecutionError",
]
