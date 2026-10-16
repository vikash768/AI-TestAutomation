"""Resolve symbolic secret placeholders only when an action is executed."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_LEGACY_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SecretResolutionError(ValueError):
    """Raised when a required execution-time secret is unavailable."""


def make_secret_placeholder(name: str) -> str:
    """Return the canonical placeholder for an environment variable name."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Secret name must be a valid environment variable name")
    return f"{{{{{name}}}}}"


class SecretResolver:
    """Resolve ``{{NAME}}`` placeholders from an environment-like mapping.

    Legacy ``${NAME}`` placeholders are accepted during migration, but all new
    plans and caches should use the canonical double-brace form.
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = environ if environ is not None else os.environ

    @staticmethod
    def is_secret_placeholder(value: Any) -> bool:
        """Return True when *value* consists of exactly one placeholder."""
        if not isinstance(value, str):
            return False
        return bool(
            _PLACEHOLDER_RE.fullmatch(value)
            or _LEGACY_PLACEHOLDER_RE.fullmatch(value)
        )

    @staticmethod
    def contains_secret_placeholder(value: Any) -> bool:
        """Return True when *value* contains a canonical or legacy placeholder."""
        if not isinstance(value, str):
            return False
        return bool(_PLACEHOLDER_RE.search(value) or _LEGACY_PLACEHOLDER_RE.search(value))

    @staticmethod
    def canonicalize(value: str) -> str:
        """Convert legacy placeholders in *value* to canonical placeholders."""
        return _LEGACY_PLACEHOLDER_RE.sub(
            lambda match: make_secret_placeholder(match.group(1)), value
        )

    def resolve(self, value: str) -> str:
        """Resolve all placeholders in a string without logging secret values."""
        canonical = self.canonicalize(value)

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = self._environ.get(name)
            if resolved is None or resolved == "":
                raise SecretResolutionError(
                    f"Required secret environment variable '{name}' is not set"
                )
            return resolved

        return _PLACEHOLDER_RE.sub(replace, canonical)

    def resolve_data(self, value: Any) -> Any:
        """Resolve placeholders recursively in strings, mappings, and sequences."""
        if isinstance(value, str):
            return self.resolve(value)
        if isinstance(value, dict):
            return {key: self.resolve_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.resolve_data(item) for item in value)
        return value
