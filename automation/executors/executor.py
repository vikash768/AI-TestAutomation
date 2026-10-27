"""Backward-compatibility alias: executor -> selenium_executor."""

from automation.executors.selenium_executor import *  # noqa: F401, F403
from automation.executors.selenium_executor import (
    ActionExecutionError,
    SeleniumExecutor,
)

__all__ = [
    "ActionExecutionError",
    "SeleniumExecutor",
]
