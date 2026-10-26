"""Action vocabulary executed by the Playwright executor.

Plans are data: a test is a list of Action dicts, so they can be stored,
inspected before running, and replayed. Credentials are never stored in a
plan; a value of "<email>" or "<password>" is substituted at run time.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class ActionType(str, enum.Enum):
    NAVIGATE = "NAVIGATE"
    CLICK = "CLICK"
    FILL = "FILL"
    PRESS = "PRESS"
    SELECT = "SELECT"
    CHECK = "CHECK"
    SET_VIEWPORT = "SET_VIEWPORT"
    RELOAD = "RELOAD"
    GO_BACK = "GO_BACK"
    WAIT = "WAIT"
    WAIT_URL = "WAIT_URL"
    EXPECT_URL_CONTAINS = "EXPECT_URL_CONTAINS"
    EXPECT_URL_NOT_CONTAINS = "EXPECT_URL_NOT_CONTAINS"
    EXPECT_VISIBLE = "EXPECT_VISIBLE"
    EXPECT_NOT_VISIBLE = "EXPECT_NOT_VISIBLE"
    EXPECT_TEXT = "EXPECT_TEXT"
    EXPECT_NO_TEXT = "EXPECT_NO_TEXT"
    EXPECT_VALUE = "EXPECT_VALUE"
    EXPECT_ENABLED = "EXPECT_ENABLED"
    EXPECT_DISABLED = "EXPECT_DISABLED"
    EXPECT_STORAGE = "EXPECT_STORAGE"
    EVAL = "EVAL"
    SCREENSHOT = "SCREENSHOT"


class Classification(str, enum.Enum):
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    LOCATOR_ERROR = "LOCATOR_ERROR"
    TIMEOUT = "TIMEOUT"
    NAVIGATION_ERROR = "NAVIGATION_ERROR"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    PLAN_ERROR = "PLAN_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class Target:
    selector: str | None = None
    role: str | None = None
    name: str | None = None
    exact: bool = True

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Action:
    type: ActionType
    description: str = ""
    target: Target | None = None
    value: str | None = None
    timeout_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"type": self.type.value, "description": self.description}
        if self.target:
            d["target"] = self.target.to_dict()
        if self.value is not None:
            d["value"] = self.value
        if self.timeout_ms:
            d["timeout_ms"] = self.timeout_ms
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        t = d.get("target")
        return cls(
            type=ActionType(d["type"]),
            description=d.get("description", ""),
            target=Target(**t) if t else None,
            value=d.get("value"),
            timeout_ms=d.get("timeout_ms"),
            extra=d.get("extra", {}),
        )
