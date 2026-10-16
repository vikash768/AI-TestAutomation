"""Secure Robot Framework keywords backed by the shared secret resolver."""

from __future__ import annotations

from typing import Any

from robot.api.deco import keyword, library
from robot.libraries.BuiltIn import BuiltIn

from automation.secrets.resolver import SecretResolutionError, SecretResolver
from automation.secrets.sanitizer import sanitize_text


class RobotSecretInputError(RuntimeError):
    """Controlled failure raised when secure Robot input cannot be completed."""


@library(scope="GLOBAL")
class SecureKeywords:
    """Resolve symbolic credentials only inside a non-logging input keyword."""

    def __init__(
        self,
        resolver: SecretResolver | None = None,
        selenium_library: Any | None = None,
    ) -> None:
        self._resolver = resolver or SecretResolver()
        self._selenium_library = selenium_library

    def _selenium(self) -> Any:
        if self._selenium_library is not None:
            return self._selenium_library
        return BuiltIn().get_library_instance("SeleniumLibrary")

    @keyword("Value Is Secret Placeholder", tags=("secret",))
    def value_is_secret_placeholder(self, value: Any) -> bool:
        """Return whether a Robot value contains a symbolic secret reference."""
        return SecretResolver.contains_secret_placeholder(value)

    @keyword("Input Secret Text", tags=("secret",))
    def input_secret_text(
        self,
        locator: str,
        secret_reference: str,
        clear: bool = True,
    ) -> None:
        """Resolve a placeholder and type it without passing the value to Robot."""
        if not SecretResolver.contains_secret_placeholder(secret_reference):
            raise RobotSecretInputError(
                "Secure credential input requires a symbolic secret placeholder."
            )

        resolved_value: str | None = None
        try:
            resolved_value = self._resolver.resolve(secret_reference)
            # Direct Python invocation avoids creating a Robot keyword event with
            # the resolved value. SeleniumLibrary's input_password additionally
            # suppresses the typed value from INFO-level Selenium logs.
            self._selenium().input_password(locator, resolved_value, clear)
        except SecretResolutionError as exc:
            raise RobotSecretInputError(sanitize_text(str(exc))) from None
        except Exception as exc:
            # Third-party driver errors can echo arguments. Keep the exception
            # category for diagnosis without including its potentially unsafe text.
            raise RobotSecretInputError(
                f"Secure credential input failed ({type(exc).__name__})."
            ) from None
        finally:
            resolved_value = None
