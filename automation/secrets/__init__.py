"""Secret placeholder resolution and safe serialization helpers."""

from automation.secrets.resolver import (
    SecretResolutionError,
    SecretResolver,
    make_secret_placeholder,
)
from automation.secrets.sanitizer import (
    REDACTED,
    is_sensitive_key,
    sanitize_data,
    sanitize_filename_component,
    sanitize_text,
    sanitize_value,
)

__all__ = [
    "REDACTED",
    "SecretResolutionError",
    "SecretResolver",
    "is_sensitive_key",
    "make_secret_placeholder",
    "sanitize_data",
    "sanitize_filename_component",
    "sanitize_text",
    "sanitize_value",
]
