"""Runtime Secret Manager reads for the verification workers.

The engine reads secrets at runtime via the VM's own service-account identity
(Application Default Credentials from the GCE metadata server) — e.g.
``hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com`` on Production,
``robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com`` on
the Test VM. NEVER read secrets from key files.

Operational note: Carlo shared a JSON key at ``~/.config/gcp/hermes-poc-key.json``
for inventory purposes. That file must NEVER be committed, copied into this
repo, or referenced by engine code. This module actively refuses key-file
credentials (see :func:`get_secret`): delete the Drive copy once runtime reads
are proven.

Fail-closed contract: any missing grant, missing secret, or missing dependency
raises :class:`SecretManagerAccessError` with a ``NEEDS_AUTH``-style message
naming the required IAM grant. Never return an empty string or None.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("robie.gcp_secret_reader")

DEFAULT_PROJECT = "streetsmart-hermes-poc"

# The inventory-only key file Carlo shared. Engine code must never read it.
FORBIDDEN_KEY_FILE = os.path.expanduser("~/.config/gcp/hermes-poc-key.json")


class SecretManagerAccessError(RuntimeError):
    """Fail-closed secret access error (NEEDS_AUTH semantics)."""


def _api_error_types() -> tuple[type[BaseException], ...]:
    try:
        from google.api_core import exceptions as api_exceptions

        return (api_exceptions.PermissionDenied, api_exceptions.NotFound)
    except ImportError:
        return ()


def _refuse_key_files() -> None:
    """Refuse key-file credentials outright.

    ``GOOGLE_APPLICATION_CREDENTIALS`` pointing at a JSON key would silently
    bypass the VM-identity requirement, so it is rejected before any client is
    built.
    """
    key_path = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not key_path:
        return
    if os.path.abspath(os.path.expanduser(key_path)) == os.path.abspath(
        FORBIDDEN_KEY_FILE
    ):
        raise SecretManagerAccessError(
            "NEEDS_AUTH: refusing to read secrets with the inventory key file "
            f"({FORBIDDEN_KEY_FILE}); the engine must authenticate as the VM's "
            "own service-account identity. Unset GOOGLE_APPLICATION_CREDENTIALS."
        )
    raise SecretManagerAccessError(
        "NEEDS_AUTH: refusing key-file credentials "
        f"({key_path}); the engine must authenticate as the VM's own "
        "service-account identity (Application Default Credentials from the "
        "GCE metadata server). Unset GOOGLE_APPLICATION_CREDENTIALS."
    )


def get_secret(name: str, *, project: str = DEFAULT_PROJECT) -> str:
    """Read the latest version of a Secret Manager secret payload as text.

    Authenticates with Application Default Credentials (the VM's attached
    service account). Fail closed: raises :class:`SecretManagerAccessError`
    with a NEEDS_AUTH-style message naming the missing IAM grant instead of
    returning an empty/None value.
    """
    secret_name = str(name or "").strip()
    project_id = str(project or "").strip() or DEFAULT_PROJECT
    if not secret_name:
        raise SecretManagerAccessError("NEEDS_AUTH: secret name must not be empty")
    _refuse_key_files()

    try:
        from google.cloud import secretmanager as secret_manager
    except ImportError as exc:
        raise SecretManagerAccessError(
            "NEEDS_AUTH: google-cloud-secret-manager is not installed in this "
            "environment; the engine cannot read Secret Manager secrets"
        ) from exc

    client: Any = secret_manager.SecretManagerServiceClient()
    resource = client.secret_version_path(project_id, secret_name, "latest")
    try:
        response = client.access_secret_version(request={"name": resource})
    except Exception as exc:
        permission_denied: tuple[type[BaseException], ...] = ()
        not_found: tuple[type[BaseException], ...] = ()
        api_errors = _api_error_types()
        if len(api_errors) == 2:
            permission_denied, not_found = (api_errors[0],), (api_errors[1],)
        if permission_denied and isinstance(exc, permission_denied):
            raise SecretManagerAccessError(
                f"NEEDS_AUTH: the runtime service account cannot read secret "
                f"'{secret_name}' in project '{project_id}'; grant "
                "roles/secretmanager.secretAccessor on that secret to the VM's "
                "service-account identity"
            ) from exc
        if not_found and isinstance(exc, not_found):
            raise SecretManagerAccessError(
                f"NEEDS_AUTH: secret '{secret_name}' was not found in project "
                f"'{project_id}'"
            ) from exc
        raise SecretManagerAccessError(
            f"NEEDS_AUTH: failed to read secret '{secret_name}' in project "
            f"'{project_id}': {type(exc).__name__}: {exc}"
        ) from exc

    payload = response.payload.data.decode("utf-8")
    if not payload:
        raise SecretManagerAccessError(
            f"NEEDS_AUTH: secret '{secret_name}' in project '{project_id}' "
            "returned an empty payload; refusing to continue"
        )
    logger.info(
        "read secret %s in project %s (%d chars)",
        secret_name,
        project_id,
        len(payload),
    )
    return payload
