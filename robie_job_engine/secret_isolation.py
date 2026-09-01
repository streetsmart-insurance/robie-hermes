"""Fail closed when Test runtime targets Production-only Secret Manager secrets.

IAM must also deny Test service accounts on Production secrets. This module is
the in-process backstop when ROBIE_ENV, hostname, or misconfiguration would
otherwise reach a Production credential.
"""

from __future__ import annotations

import os
import socket

from .runtime_env import TEST_ENV_NAME, current_robie_env


class SecretIsolationError(RuntimeError):
    """Test attempted to read a Production-only secret."""


PRODUCTION_ONLY_SECRET_IDS = frozenset(
    {
        "ezlynx-username",
        "ezlynx-password",
        "robie-google-oauth-token",
    }
)

TEST_HOST_PREFIXES = ("hermes-test-01",)


def _hostname() -> str:
    return socket.gethostname().strip().lower()


def is_test_runtime() -> bool:
    if current_robie_env() == TEST_ENV_NAME:
        return True
    host = _hostname()
    return any(host == prefix or host.startswith(f"{prefix}.") for prefix in TEST_HOST_PREFIXES)


def production_secret_id(reference_or_parent: str) -> str | None:
    text = str(reference_or_parent or "").strip()
    if "/versions/" in text:
        text = text.rsplit("/versions/", 1)[0]
    if "/secrets/" not in text:
        return None
    secret_id = text.rsplit("/secrets/", 1)[-1].strip()
    if secret_id in PRODUCTION_ONLY_SECRET_IDS:
        return secret_id
    return None


def forbid_test_reading_production_secrets(reference_or_parent: str) -> None:
    """Raise when Test host/env would read a Production-only secret."""
    if not is_test_runtime():
        return
    secret_id = production_secret_id(reference_or_parent)
    if secret_id is None:
        return
    raise SecretIsolationError(
        f"Test runtime on {_hostname()} is forbidden from reading Production secret "
        f"{secret_id}; use Test-only secrets and IAM isolation"
    )
