from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .secret_resolution import access_newest_enabled_secret, secret_parent


class SecretAccessor(Protocol):
    def access(self, resource_name: str) -> str: ...
    def access_newest_enabled(self, reference_or_parent: str) -> str: ...


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

    def access_newest_enabled(self, reference_or_parent: str) -> str:
        return access_newest_enabled_secret(self._client, reference_or_parent)


@dataclass(frozen=True, repr=False)
class EzlynxCredentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "EzlynxCredentials(username=<redacted>, password=<redacted>)"


def load_ezlynx_credentials(accessor: SecretAccessor | None = None) -> EzlynxCredentials:
    username_ref = os.environ.get("ROBIE_EZLYNX_USERNAME_SECRET", "").strip()
    password_ref = os.environ.get("ROBIE_EZLYNX_PASSWORD_SECRET", "").strip()
    if not username_ref or not password_ref:
        raise RuntimeError(
            "ROBIE_EZLYNX_USERNAME_SECRET and ROBIE_EZLYNX_PASSWORD_SECRET must be configured"
        )
    accessor = accessor or GoogleSecretManagerAccessor()
    username = accessor.access_newest_enabled(username_ref)
    password = accessor.access_newest_enabled(password_ref)
    return EzlynxCredentials(username=username, password=password)
