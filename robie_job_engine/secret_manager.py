from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


class SecretAccessor(Protocol):
    def access(self, resource_name: str) -> str: ...


def normalize_secret_version_ref(
    resource_name: str,
    project_id: str | None = None,
) -> str:
    """Normalize a Secret Manager reference to a full version resource name.

    Accepts:
      - Full resource name: projects/<p>/secrets/<s>/versions/<v>
      - Secret-level name: projects/<p>/secrets/<s> -> adds /versions/latest
      - Bare secret name: <s> -> adds projects/<project_id>/secrets/<s>/versions/latest
    """
    ref = (resource_name or "").strip()
    if not ref:
        raise ValueError("Secret Manager reference cannot be empty")
    if ref.startswith("projects/") and "/versions/" in ref:
        return ref
    project = (
        project_id
        or os.environ.get("PROJECT_ID")
        or os.environ.get("GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "streetsmart-hermes-poc"
    )
    if ref.startswith("projects/") and "/secrets/" in ref:
        return f"{ref}/versions/latest"
    return f"projects/{project}/secrets/{ref}/versions/latest"


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
        name = normalize_secret_version_ref(resource_name)
        if not name.startswith("projects/") or "/versions/" not in name:
            raise ValueError("Secret Manager reference must be a full secret-version resource name")
        response = self._client.access_secret_version(request={"name": name})
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
    username_ref = os.environ.get("ROBIE_EZLYNX_USERNAME_SECRET", "").strip()
    password_ref = os.environ.get("ROBIE_EZLYNX_PASSWORD_SECRET", "").strip()
    if not username_ref or not password_ref:
        raise RuntimeError(
            "ROBIE_EZLYNX_USERNAME_SECRET and ROBIE_EZLYNX_PASSWORD_SECRET must be configured"
        )
    accessor = accessor or GoogleSecretManagerAccessor()
    username = accessor.access(username_ref)
    password = accessor.access(password_ref)
    return EzlynxCredentials(username=username, password=password)
