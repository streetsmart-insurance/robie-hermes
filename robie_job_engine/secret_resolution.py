"""Resolve Secret Manager secrets to the newest ENABLED version.

Production must never read ``versions/latest`` or a pinned numeric version when
that version is DESTROYED while an older ENABLED version still exists. EZLynx
login and preflight use this module; payloads are never logged.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_PARENT_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/secrets/(?P<secret_id>[^/]+)$"
)


def secret_parent(reference: str) -> str:
    """Normalize a secret or version reference to ``projects/.../secrets/name``."""
    text = str(reference or "").strip()
    if not text:
        raise ValueError("Secret Manager reference is required")
    if "/versions/" in text:
        text = text.rsplit("/versions/", 1)[0]
    match = _SECRET_PARENT_RE.fullmatch(text)
    if not match:
        raise ValueError(
            "Secret Manager reference must be projects/<project>/secrets/<name> "
            "or projects/<project>/secrets/<name>/versions/<id>"
        )
    return text


def _create_time_key(version: Any) -> float:
    created = getattr(version, "create_time", 0)
    if hasattr(created, "timestamp") and callable(created.timestamp):
        try:
            return float(created.timestamp())
        except Exception:
            pass
    seconds = getattr(created, "seconds", None)
    if seconds is not None:
        nanos = getattr(created, "nanos", 0) or 0
        return float(seconds) + float(nanos) / 1e9
    try:
        return float(created)
    except (TypeError, ValueError):
        return 0.0


def _version_suffix(name: str) -> str:
    text = str(name or "")
    if "/versions/" in text:
        return "versions/" + text.rsplit("/versions/", 1)[-1]
    return text or "versions/unknown"


def newest_enabled_version_name(client: Any, parent: str) -> str:
    """Return the full resource name of the newest ENABLED version."""
    parent = secret_parent(parent)
    enabled = list(
        client.list_secret_versions(
            request={"parent": parent, "filter": "state:ENABLED"}
        )
    )
    if not enabled:
        raise RuntimeError(f"No enabled version exists for required secret {parent}")
    newest = max(enabled, key=_create_time_key)
    return str(newest.name)


def access_newest_enabled_secret(client: Any, reference_or_parent: str) -> str:
    """Read the newest ENABLED payload; ignores stale ``latest`` or pinned version suffixes."""
    version_name = newest_enabled_version_name(client, reference_or_parent)
    response = client.access_secret_version(request={"name": version_name})
    value = response.payload.data.decode("utf-8").strip()
    if not value or "\x00" in value:
        raise ValueError("Secret Manager returned an empty or invalid credential")
    return value


def describe_newest_enabled_resolution(
    client: Any,
    reference_or_parent: str,
) -> dict[str, str]:
    """State-only resolution proof for operators and evidence dumps."""
    parent = secret_parent(reference_or_parent)
    version_name = newest_enabled_version_name(client, parent)
    all_versions = list(client.list_secret_versions(request={"parent": parent}))
    newest_overall = (
        max(all_versions, key=_create_time_key) if all_versions else None
    )
    return {
        "secret_parent": parent,
        "requested_reference": str(reference_or_parent or "").strip(),
        "resolved_enabled_version": _version_suffix(version_name),
        "newest_version_overall": (
            _version_suffix(newest_overall.name) if newest_overall is not None else "none"
        ),
    }
