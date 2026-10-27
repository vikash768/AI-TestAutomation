"""Actions package (deprecated alias) — re-exports from automation.executors."""

from automation.executors import *  # noqa: F401, F403
from automation.executors import (
    ActionExecutionError,
    AppiumExecutor,
    SeleniumExecutor,
)

__all__ = [
    "SeleniumExecutor",
    "AppiumExecutor",
    "ActionExecutionError",
]
