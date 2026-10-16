"""Central redaction helpers for plans, caches, results, and logs."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from automation.secrets.resolver import SecretResolver, make_secret_placeholder


REDACTED = "<redacted>"

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
)

_URL_USERINFO_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@/\s]+)@"
)
_SENSITIVE_PARAMETER_RE = re.compile(
    r"(?i)([?&](?:password|passwd|secret|token|(?:api|access|auth)[_-]?token|api[_-]?key|apikey|authorization|credential)=)([^&#\s]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)([^\s,;]+)"
)


def _normalized_key(key: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_sensitive_key(key: Any) -> bool:
    """Return True for credential-bearing key names and common variants."""
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_credential_identity_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized.endswith("_email") and any(
        marker in normalized for marker in ("user", "admin", "login", "valid")
    )


def _known_secret_values(
    environ: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    source = environ if environ is not None else os.environ
    items = [
        (name, value)
        for name, value in source.items()
        if value
        and len(value) >= 4
        and (is_sensitive_key(name) or _is_credential_identity_key(name))
    ]
    return sorted(items, key=lambda item: len(item[1]), reverse=True)


def sanitize_text(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Replace known environment secrets in text with symbolic placeholders."""
    if not isinstance(value, str):
        return value
    sanitized = _URL_USERINFO_RE.sub(r"\1<redacted>:<redacted>@", value)
    sanitized = _SENSITIVE_PARAMETER_RE.sub(r"\1<redacted>", sanitized)
    sanitized = _AUTHORIZATION_RE.sub(r"\1<redacted>", sanitized)
    for name, secret in _known_secret_values(environ):
        if secret in sanitized:
            sanitized = sanitized.replace(secret, make_secret_placeholder(name))
    return sanitized


def sanitize_filename_component(value: Any, *, default: str = "artifact") -> str:
    """Return a secret-safe, path-safe filename component."""
    sanitized = str(sanitize_text(value) or "")
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", sanitized).strip("._-")
    sanitized = re.sub(r"\.{2,}", "_", sanitized)
    return sanitized[:120] or default


def sanitize_value(
    value: Any,
    *,
    key: Any = None,
    sensitive: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Sanitize one value, preserving safe symbolic placeholders."""
    if isinstance(value, str):
        sanitized = sanitize_text(value, environ=environ)
        if sensitive or is_sensitive_key(key):
            if SecretResolver.contains_secret_placeholder(sanitized):
                return sanitized
            return REDACTED
        return sanitized
    if sensitive or is_sensitive_key(key):
        return REDACTED
    return value


def sanitize_data(
    value: Any,
    *,
    key: Any = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Recursively sanitize mappings and sequences for safe persistence."""
    if is_sensitive_key(key):
        return sanitize_value(value, key=key, environ=environ)
    if isinstance(value, dict):
        return {
            item_key: sanitize_data(item, key=item_key, environ=environ)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_data(item, key=key, environ=environ) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_data(item, key=key, environ=environ) for item in value)
    return sanitize_value(value, key=key, environ=environ)
