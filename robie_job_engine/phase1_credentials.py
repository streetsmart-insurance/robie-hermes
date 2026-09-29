"""Phase 1 document-puller: runtime credential loading (read-only worker).

Credentials are NEVER hardcoded and NEVER logged. Each carrier adapter
declares two environment variable names; at runtime those variables hold
Secret Manager resource references (``projects/.../secrets/.../versions/...``),
following the ``ROBIE_EZLYNX_USERNAME_SECRET`` pattern in
``robie_job_engine/secret_manager.py``. Payloads are read through the
injected ``SecretAccessor`` (production: GoogleSecretManagerAccessor with
ADC; tests: a fake).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class CredentialConfigurationError(RuntimeError):
    """Raised when a credential env var is missing or the payload is unusable."""


@dataclass(frozen=True, repr=False)
class PortalCredentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never leak values into logs
        return "PortalCredentials(username=<redacted>, password=<redacted>)"


def load_portal_credentials(
    username_env: str,
    password_env: str,
    accessor=None,
) -> PortalCredentials:
    """Load one carrier's portal credentials at runtime.

    ``username_env`` / ``password_env`` name environment variables whose
    values are Secret Manager resource references. Raises
    CredentialConfigurationError when anything is missing or empty.
    """
    username_ref = os.environ.get(username_env, "").strip()
    password_ref = os.environ.get(password_env, "").strip()
    if not username_ref or not password_ref:
        raise CredentialConfigurationError(
            f"{username_env} and {password_env} must both be configured "
            "(Secret Manager resource references)"
        )
    if accessor is None:
        from .secret_manager import GoogleSecretManagerAccessor

        accessor = GoogleSecretManagerAccessor()
    username = accessor.access(username_ref)
    password = accessor.access(password_ref)
    if not username or not password:
        raise CredentialConfigurationError(
            "Secret Manager returned an empty credential payload"
        )
    return PortalCredentials(username=username, password=password)
