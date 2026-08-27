"""List EZLynx Secret Manager version *states*. Never read payloads.

Job 6cf6f6ae HITL'd when the newest password version was DESTROYED. Live
login (`ezlynx_login_bootstrap.secret`) uses newest ENABLED by create_time,
not ``versions/latest``. Job 468d1575 then ALERTed on DESTROYED password v2
while ENABLED v1 still logged in. This module ALERTs / HOLDs only when there
is no ENABLED version. A DESTROYED ``versions/latest`` leftover is healthy
when an older ENABLED version exists.

No EZLynx-side password-rotation webhook exists in this repo. Do not invent
one. Pawel owns rotation. Never print or store the password.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from .secrets import redact_mapping


logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "streetsmart-hermes-poc"
DEFAULT_SECRETS = ("ezlynx-username", "ezlynx-password")
CHECKPOINT = "login_secret_health"
SENTINEL_IDEMPOTENCY = "login_secret_health"
STATE_MAP = {0: "UNSPECIFIED", 1: "ENABLED", 2: "DISABLED", 3: "DESTROYED"}
COOLDOWN_SECONDS = 1800


def _project() -> str:
    return (
        os.environ.get("ROBIE_SECRET_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or DEFAULT_PROJECT
    ).strip()


def _secret_names() -> tuple[str, ...]:
    raw = os.environ.get("ROBIE_LOGIN_SECRET_NAMES", "").strip()
    if not raw:
        return DEFAULT_SECRETS
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    return names or DEFAULT_SECRETS


def _state_name(version: Any) -> str:
    raw = getattr(version, "state", "")
    name = getattr(raw, "name", None)
    if name:
        return str(name).rsplit(".", 1)[-1].upper()
    text = str(raw).rsplit(".", 1)[-1].upper()
    if text.isdigit():
        return STATE_MAP.get(int(text), text)
    return text or "UNKNOWN"


def _sort_key(version: Any) -> float:
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


def _version_id(name: str) -> str:
    text = str(name or "")
    if "/versions/" in text:
        return "versions/" + text.rsplit("/versions/", 1)[-1]
    return text or "versions/unknown"


def summarize_secret_versions(versions: list[Any], *, secret_id: str) -> dict[str, Any]:
    """Build a state-only report for one secret. Never includes payloads."""
    rows = [
        {
            "version": _version_id(getattr(item, "name", "")),
            "state": _state_name(item),
            "create_time": _sort_key(item),
        }
        for item in versions
    ]
    rows.sort(key=lambda item: item["create_time"], reverse=True)
    enabled = [item for item in rows if item["state"] == "ENABLED"]
    newest = rows[0] if rows else None
    newest_enabled = enabled[0] if enabled else None
    missing_enabled = not enabled
    newest_destroyed = bool(newest and newest["state"] == "DESTROYED")
    leftover_destroyed = newest_destroyed and not missing_enabled
    # Align with ezlynx_login_bootstrap.secret: newest ENABLED is healthy.
    # DESTROYED latest must not ALERT when an older ENABLED version exists.
    alert = missing_enabled
    return {
        "secret_id": secret_id,
        "versions": [{"version": item["version"], "state": item["state"]} for item in rows],
        "enabled_versions": [item["version"] for item in enabled],
        "newest_enabled_version": None if newest_enabled is None else newest_enabled["version"],
        "newest_version": None if newest is None else newest["version"],
        "newest_state": None if newest is None else newest["state"],
        "missing_enabled": missing_enabled,
        "newest_destroyed": newest_destroyed,
        "leftover_destroyed": leftover_destroyed,
        "alert": alert,
    }


def inspect_login_secrets(
    *,
    client: Any | None = None,
    project: str | None = None,
    secret_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """List version states. Never calls access_secret_version."""
    project_id = project or _project()
    names = secret_ids or _secret_names()
    if client is None:
        try:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
        except Exception as exc:
            return {
                "result": "UNKNOWN",
                "reason": f"secret manager unavailable: {type(exc).__name__}",
                "project": project_id,
                "secrets": [],
                "should_hold": False,
            }
    secrets: list[dict[str, Any]] = []
    errors: list[str] = []
    for secret_id in names:
        parent = f"projects/{project_id}/secrets/{secret_id}"
        try:
            listed = list(client.list_secret_versions(request={"parent": parent}))
        except Exception as exc:
            errors.append(f"{secret_id}:{type(exc).__name__}")
            secrets.append(
                {
                    "secret_id": secret_id,
                    "versions": [],
                    "enabled_versions": [],
                    "newest_enabled_version": None,
                    "newest_version": None,
                    "newest_state": None,
                    "missing_enabled": True,
                    "newest_destroyed": False,
                    "leftover_destroyed": False,
                    "alert": True,
                    "error": type(exc).__name__,
                }
            )
            continue
        secrets.append(summarize_secret_versions(listed, secret_id=secret_id))
    alerting = [item for item in secrets if item.get("alert")]
    should_hold = any(item.get("missing_enabled") for item in secrets)
    if errors and not any(item.get("versions") for item in secrets):
        result = "UNKNOWN"
        reason = "secret manager list failed (" + ",".join(errors) + ")"
    elif alerting:
        result = "ALERT"
        reason = "; ".join(
            f"{item['secret_id']} "
            + (
                "has no ENABLED version"
                if item.get("missing_enabled")
                else f"newest {item.get('newest_version')} is DESTROYED"
            )
            for item in alerting
        )
    else:
        result = "OK"
        leftover = format_leftover_note({"secrets": secrets})
        reason = leftover or "each watched secret has an ENABLED version"
    return {
        "result": result,
        "reason": reason,
        "project": project_id,
        "secrets": secrets,
        "should_hold": should_hold,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def format_leftover_note(report: dict[str, Any]) -> str:
    """Chat wording for DESTROYED latest + ENABLED older. Names/states only."""
    notes: list[str] = []
    for item in report.get("secrets") or []:
        if not item.get("leftover_destroyed"):
            continue
        enabled = item.get("newest_enabled_version") or (
            (item.get("enabled_versions") or [None])[0]
        )
        newest = item.get("newest_version") or "versions/unknown"
        if not enabled:
            continue
        notes.append(
            f"{item.get('secret_id')}: using ENABLED {enabled}; "
            f"{newest} is DESTROYED leftover"
        )
    return "; ".join(notes)


def format_alert(report: dict[str, Any]) -> str:
    """Chat / ops text. States and version names only. No payloads."""
    leftover = format_leftover_note(report)
    lines = [
        f"ROBIE login-secret alert — {report.get('project') or _project()}",
        str(report.get("reason") or leftover or "login secret version state is unsafe"),
    ]
    for item in report.get("secrets") or []:
        enabled = ",".join(item.get("enabled_versions") or []) or "none"
        newest_enabled = item.get("newest_enabled_version") or "none"
        if item.get("leftover_destroyed"):
            lines.append(
                f"secret {item.get('secret_id')} using ENABLED {newest_enabled}; "
                f"{item.get('newest_version') or 'none'} is DESTROYED leftover; "
                f"enabled={enabled}"
            )
            continue
        lines.append(
            f"secret {item.get('secret_id')} newest={item.get('newest_version') or 'none'} "
            f"state={item.get('newest_state') or 'UNKNOWN'}; enabled={enabled}"
        )
    lines.extend(
        (
            "DESTROYED versions cannot be restored. Add a new ENABLED version "
            "(Pawel owns rotation). Then reply RETRY in the Robie Chat HITL thread.",
            "Chat RETRY does not change the version; bootstrap re-reads newest ENABLED on resume.",
            "Never print or store the password.",
        )
    )
    return "\n".join(lines)


def _fingerprint(report: dict[str, Any]) -> str:
    """Compact state token. Must survive JobStore secret redaction."""
    parts = [str(report.get("result") or "")]
    for item in sorted(report.get("secrets") or [], key=lambda row: str(row.get("secret_id") or "")):
        parts.append(
            f"{item.get('newest_version') or 'none'}.{item.get('newest_state') or 'none'}."
            f"{'+'.join(item.get('enabled_versions') or ['none'])}"
        )
    return "/".join(parts)


def _emit_alert(
    text: str,
    *,
    conversation_id: str | None,
    poster: Callable[..., Any] | None,
) -> bool:
    space = str(conversation_id or "").strip()
    if not space.startswith("spaces/"):
        space = (
            os.environ.get("ROBIE_LOGIN_SECRET_ALERT_SPACE")
            or os.environ.get("ROBIE_OPS_CHAT_SPACE")
            or ""
        ).strip()
    if not space.startswith("spaces/"):
        return False
    send = poster
    if send is None:
        from .chat_app_post import post_as_chat_app

        send = post_as_chat_app
    try:
        send(space, text)
        return True
    except Exception:
        logger.exception("login-secret Chat alert failed")
        return False


def maybe_preflight_login_secrets(
    store: Any,
    job: dict[str, Any],
    *,
    inspector: Callable[..., dict[str, Any]] | None = None,
    poster: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run before a Chat job goes RUNNING. Hold only when no ENABLED version."""
    from .models import JobStatus

    inspect = inspector or inspect_login_secrets
    report = inspect()
    store.checkpoint(job["id"], CHECKPOINT, redact_mapping(dict(report)))
    if report.get("result") != "ALERT":
        return report
    text = format_alert(report)
    payload = dict(job.get("payload") or {})
    posted = _emit_alert(
        text,
        conversation_id=str(payload.get("conversation_id") or ""),
        poster=poster,
    )
    report = dict(report)
    report["alert_text"] = text
    report["chat_posted"] = posted
    if report.get("should_hold"):
        current = store.get_job(job["id"])
        if current["status"] == JobStatus.PENDING.value:
            store.transition(
                job["id"],
                JobStatus.NEEDS_AUTH,
                expected={JobStatus.PENDING},
                error=text,
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
    return report


def maybe_periodic_login_secret_check(
    db_path: str,
    *,
    inspector: Callable[..., dict[str, Any]] | None = None,
    poster: Callable[..., Any] | None = None,
    now: datetime | None = None,
    cooldown_seconds: int | None = None,
) -> dict[str, Any]:
    """Scheduler tick. No-ops when Secret Manager is unavailable (CI / Test)."""
    from .store import JobStore

    inspect = inspector or inspect_login_secrets
    report = inspect()
    if report.get("result") == "UNKNOWN":
        return report
    store = JobStore(db_path)
    sentinel = store.create_job(
        "hermes.plain_english",
        {"purpose": "login_secret_health"},
        idempotency_key=SENTINEL_IDEMPOTENCY,
    )
    previous = store.get_checkpoint(sentinel["id"], CHECKPOINT) or {}
    checked = now or datetime.now(timezone.utc)
    wait = cooldown_seconds
    if wait is None:
        wait = int(os.environ.get("ROBIE_LOGIN_SECRET_ALERT_COOLDOWN_SECONDS") or COOLDOWN_SECONDS)
    last_at = str(previous.get("alerted_at") or "")
    same = previous.get("fingerprint") == _fingerprint(report)
    if report.get("result") == "ALERT" and same and last_at:
        try:
            elapsed = (
                checked - datetime.fromisoformat(last_at)
            ).total_seconds()
        except ValueError:
            elapsed = wait + 1
        if elapsed < wait:
            store.checkpoint(
                sentinel["id"],
                CHECKPOINT,
                redact_mapping({**report, "suppressed": True, "fingerprint": _fingerprint(report)}),
            )
            return report
    posted = False
    if report.get("result") == "ALERT":
        posted = _emit_alert(format_alert(report), conversation_id=None, poster=poster)
    payload = dict(report)
    payload["fingerprint"] = _fingerprint(report)
    payload["chat_posted"] = posted
    if report.get("result") == "ALERT":
        payload["alerted_at"] = checked.isoformat()
    store.checkpoint(sentinel["id"], CHECKPOINT, redact_mapping(payload))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List EZLynx login secret version states. Never prints payloads."
    )
    parser.add_argument("--project", default="")
    args = parser.parse_args()
    report = inspect_login_secrets(project=args.project or None)
    print(json.dumps(report, sort_keys=True, default=str))
    if report.get("result") == "ALERT":
        return 2
    if report.get("result") == "UNKNOWN":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
