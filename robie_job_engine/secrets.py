"""Secret-redaction middleware for model-visible data, logs, and traces.

Never store, echo, or reproduce real credentials. Tests use the fake
sentinel ``REDACTED_TEST_SECRET`` only.
"""

from __future__ import annotations

import logging
import re
from typing import Any


FAKE_SECRET_SENTINEL = "REDACTED_TEST_SECRET"
REDACTED = "[REDACTED]"
SCREENSHOT_FORBIDDEN = "screenshots that may contain secrets must not be logged"

_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "set-cookie",
        "set_cookie",
        "mfa",
        "otp",
        "totp",
        "mfa_code",
        "one_time_code",
        "session",
        "sessionid",
        "bearer",
        "private_key",
        "client_secret",
        "clientsecret",
        "credential",
        "credentials",
        "fein",
        "federal_employer_identification_number",
    }
)
_PARTIAL_KEY = re.compile(
    r"(password|passwd|secret|token|cookie|authorization|mfa|otp|bearer|api[_-]?key|fein|federal[_-]?employer[_-]?identification[_-]?number)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|authorization|cookie|mfa|otp|bearer|api[_-]?key|fein|federal[_-]?employer[_-]?identification[_-]?number)"
    r"\s*[:=]\s*([^\s,;]{2,})"
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?.+"
)


def is_secret_key(name: str) -> bool:
    normalized = str(name or "").strip().casefold().replace("-", "_")
    if normalized in _SECRET_KEYS:
        return True
    return bool(_PARTIAL_KEY.search(normalized))


def redact_text(value: str) -> str:
    text = str(value)
    text = text.replace(FAKE_SECRET_SENTINEL, REDACTED)
    text = _AUTHORIZATION_HEADER.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)


def redact_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, item in (payload or {}).items():
        if is_secret_key(str(key)):
            cleaned[str(key)] = REDACTED
        else:
            cleaned[str(key)] = redact_value(item)
    return cleaned


def redact_tool_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Strip secrets from model-visible tool arguments."""
    return redact_mapping(dict(args or {}))


def redact_narration(text: str) -> str:
    return redact_text(text)


def redact_exception(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")


def contains_secret(value: Any, secret: str = FAKE_SECRET_SENTINEL) -> bool:
    if not secret:
        return False
    if isinstance(value, bytes):
        return secret.encode() in value
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(contains_secret(item, secret) for item in value.values()) or any(
            contains_secret(str(key), secret) for key in value
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item, secret) for item in value)
    return secret in str(value)


def screenshot_may_be_logged(metadata: dict[str, Any] | None) -> bool:
    """Never log screenshots tagged as containing secrets or login/MFA UI."""
    meta = {str(key).casefold(): item for key, item in dict(metadata or {}).items()}
    if meta.get("contains_secrets") or meta.get("has_secret"):
        return False
    page = str(meta.get("page") or meta.get("title") or "").casefold()
    if any(token in page for token in ("login", "password", "mfa", "sign in")):
        return False
    return True


class RedactingLogger:
    """Safe logging API that never echoes secrets."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("robie.redacting")
        if logger is None:
            self.logger.addHandler(logging.NullHandler())
            self.logger.propagate = False
        self.records: list[str] = []

    def _emit(self, level: int, message: str, *args: Any) -> str:
        rendered = message % args if args else message
        cleaned = redact_text(rendered)
        if contains_secret(cleaned):
            cleaned = REDACTED
        self.records.append(cleaned)
        self.logger.log(level, "%s", cleaned)
        return cleaned

    def info(self, message: str, *args: Any) -> str:
        return self._emit(logging.INFO, message, *args)

    def error(self, message: str, *args: Any) -> str:
        return self._emit(logging.ERROR, message, *args)

    def exception(self, exc: BaseException) -> str:
        return self._emit(logging.ERROR, redact_exception(exc))
