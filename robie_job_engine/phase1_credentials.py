"""Phase 1 document-puller: runtime credential loading (read-only worker).

Credentials are NEVER hardcoded and NEVER logged. Each carrier adapter
declares two environment variable names; at runtime those variables hold
Secret Manager resource references (``projects/.../secrets/.../versions/...``),
following the ``ROBIE_EZLYNX_USERNAME_SECRET`` pattern in
``robie_job_engine/secret_manager.py``. Payloads are read through the
injected ``SecretAccessor`` (production: GoogleSecretManagerAccessor with
ADC; tests: a fake).

``credential_preflight`` runs BEFORE any browser work: it checks that
each portal carrier's secret refs EXIST using metadata only
(``GoogleSecretMetadata.secret_exists`` calls ``get_secret``, never
``access_secret_version`` — values are never read, never logged). Any
missing ref fails the run closed: no browser is launched, no adapter
code executes, every portal policy is recorded failed with the reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Protocol


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


class SecretMetadata(Protocol):
    """Metadata-only existence check for a secret ref. Never reads values."""

    def secret_exists(self, resource_name: str) -> bool: ...


class GoogleSecretMetadata:
    """Production SecretMetadata: ``get_secret`` returns metadata only.

    NEVER calls ``access_secret_version`` — the payload (the actual
    credential value) is never fetched, never held, never logged.
    """

    def __init__(self, client=None):
        if client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise RuntimeError(
                    "google-cloud-secret-manager is required to verify "
                    "credential refs"
                ) from exc
            client = secretmanager.SecretManagerServiceClient()
        self._client = client

    def secret_exists(self, resource_name: str) -> bool:
        name = (resource_name or "").strip()
        if not name.startswith("projects/") or "/secrets/" not in name:
            return False
        # get_secret takes the parent (no /versions/ suffix) and returns
        # metadata only — no payload.
        parent = name.rsplit("/versions/", 1)[0] if "/versions/" in name else name
        try:
            self._client.get_secret(request={"name": parent})
            return True
        except Exception:
            return False  # NotFound, PermissionDenied, or unreachable


def credential_preflight(
    policies: list[dict],
    registry: dict,
    secret_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Check every portal carrier's credential refs EXIST (metadata only).

    Returns a list of human-readable missing items naming carriers and
    env var names — never secret values. Empty list = preflight passed.

    Adapters that declare no credential env vars (the ``api`` runtime,
    test fakes) are skipped. Each carrier is checked once even when it
    covers several policies.
    """
    missing: list[str] = []
    checked: set[str] = set()
    metadata = None  # constructed lazily; construction failure fails closed
    for row in policies:
        carrier_id = row.get("carrier_id")
        if not carrier_id or carrier_id in checked:
            continue
        checked.add(carrier_id)
        spec = registry[carrier_id].ADAPTER
        if spec.runtime == "api":
            continue
        if not spec.username_env and not spec.password_env:
            continue  # nothing declared (test fakes) — nothing to check
        refs: dict[str, str] = {}
        broken = False
        for env in (spec.username_env, spec.password_env):
            if not env:
                missing.append(f"{carrier_id}: credential env var not declared")
                broken = True
                continue
            ref = os.environ.get(env, "").strip()
            if not ref:
                missing.append(f"{carrier_id}: {env} is not set")
                broken = True
                continue
            refs[env] = ref
        if broken:
            continue
        if secret_exists is None and metadata is None:
            try:
                metadata = GoogleSecretMetadata().secret_exists
            except Exception as exc:
                missing.append(
                    f"{carrier_id}: cannot verify secret refs "
                    f"(secretmanager unavailable: {exc})"
                )
                continue
        checker = secret_exists if secret_exists is not None else metadata
        for env, ref in refs.items():
            try:
                exists = checker(ref)
            except Exception:
                exists = False
            if not exists:
                missing.append(
                    f"{carrier_id}: {env} secret not found in Secret Manager"
                )
    return missing
