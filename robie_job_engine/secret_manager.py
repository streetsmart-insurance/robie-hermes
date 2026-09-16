from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class SecretAccessor(Protocol):
    def access(self, resource_name: str) -> str: ...


class GoogleSecretManagerAccessor:
    """Read secret payloads with Application Default Credentials."""

    def __init__(self, client=None):
        if client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise RuntimeError(
                    "google-cloud-secret-manager is required for EZLynx credentials"
                ) from exc
            client = secretmanager.SecretManagerServiceClient()
        self._client = client

    def access(self, resource_name: str) -> str:
        if not resource_name.startswith("projects/") or "/versions/" not in resource_name:
            raise ValueError("Secret Manager reference must be a full secret-version resource name")
        response = self._client.access_secret_version(request={"name": resource_name})
        value = response.payload.data.decode("utf-8")
        if not value or "\x00" in value:
            raise ValueError("Secret Manager returned an empty or invalid credential")
        return value


@dataclass(frozen=True, repr=False)
class EzlynxCredentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "EzlynxCredentials(username=<redacted>, password=<redacted>)"


def load_ezlynx_credentials(accessor: SecretAccessor | None = None) -> EzlynxCredentials:
    """Load SSRobie login creds from newest ENABLED versions.

    Env refs may be stale pins (Test historically kept password at
    ``versions/1``). Resolve each secret's parent and access newest ENABLED.
    """
    username_ref = os.environ.get("ROBIE_EZLYNX_USERNAME_SECRET", "").strip()
    password_ref = os.environ.get("ROBIE_EZLYNX_PASSWORD_SECRET", "").strip()
    if not username_ref or not password_ref:
        raise RuntimeError(
            "ROBIE_EZLYNX_USERNAME_SECRET and ROBIE_EZLYNX_PASSWORD_SECRET must be configured"
        )
    accessor = accessor or GoogleSecretManagerAccessor()
    username = accessor.access(_newest_enabled_resource(username_ref, accessor))
    password = accessor.access(_newest_enabled_resource(password_ref, accessor))
    return EzlynxCredentials(username=username, password=password)


def _secret_parent(resource_name: str) -> str:
    if not resource_name.startswith("projects/") or "/secrets/" not in resource_name:
        raise ValueError("Secret Manager reference must be a full secret-version resource name")
    if "/versions/" in resource_name:
        return resource_name.rsplit("/versions/", 1)[0]
    return resource_name


def _newest_enabled_resource(resource_name: str, accessor: SecretAccessor) -> str:
    """Return newest ENABLED version resource; fall back to the given pin."""
    parent = _secret_parent(resource_name)
    client = getattr(accessor, "_client", None)
    if client is None:
        return resource_name
    try:
        enabled = list(
            client.list_secret_versions(
                request={"parent": parent, "filter": "state:ENABLED"}
            )
        )
    except Exception:
        return resource_name
    if not enabled:
        return resource_name
    newest = max(enabled, key=lambda version: getattr(version, "create_time", 0))
    return str(newest.name)

