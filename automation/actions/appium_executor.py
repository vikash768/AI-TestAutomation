"""Actions appium_executor (deprecated alias) — re-exports from automation.executors.appium_executor."""

from automation.executors.appium_executor import *  # noqa: F401, F403
from automation.executors.appium_executor import AppiumExecutor

__all__ = [
    "AppiumExecutor",
]
