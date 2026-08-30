"""Redacted connection readiness checks for accountability reporting."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping


def _env_ready(environment: Mapping[str, str], names: tuple[str, ...]) -> bool:
    return all(bool(environment.get(name, "").strip()) for name in names)


def check_connections(manifest_path: str, *, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    environment = environment or os.environ
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        return {"ready": False, "manifest": str(path), "error": "manifest not found", "connections": {}}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    sources = dict(manifest.get("sources") or {})
    trackers = dict(sources.get("trackers") or {})

    def file_state(key: str) -> dict[str, Any]:
        raw = sources.get(key)
        target = Path(str(raw)).expanduser() if raw else None
        return {
            "configured": bool(raw),
            "available": bool(target and target.is_file()),
            "path": str(target) if target else None,
        }

    ringcentral_export = file_state("ringcentral")
    ringcentral_api = _env_ready(environment, ("RINGCENTRAL_CLIENT_ID", "RINGCENTRAL_CLIENT_SECRET", "RINGCENTRAL_JWT"))
    ringcentral_email = dict(((manifest.get("collection") or {}).get("ringcentral_email") or {}))
    ringcentral_email_ready = bool(
        ringcentral_email.get("enabled")
        and (ringcentral_email.get("mailbox") or environment.get("ACCOUNTABILITY_REPORT_MAILBOX"))
        and environment.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT")
    )
    ezlynx_exports = {key: file_state(key) for key in ("tasks", "activities", "retention", "submissions")}
    ezlynx_browser = _env_ready(environment, ("ROBIE_EZLYNX_USERNAME_SECRET", "ROBIE_EZLYNX_PASSWORD_SECRET"))
    gmail_backend = (
        _env_ready(environment, ("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "ACCOUNTABILITY_GMAIL_USERS"))
        or _env_ready(environment, ("ACCOUNTABILITY_GMAIL_TOKEN_PATH", "ACCOUNTABILITY_GMAIL_USERS"))
    )
    appsheet_enabled = bool((manifest.get("appsheet") or {}).get("enabled"))
    appsheet_ready = _env_ready(environment, ("APPSHEET_APP_ID", "APPSHEET_APPLICATION_ACCESS_KEY"))
    google_sheets = dict(manifest.get("google_sheets") or {})
    google_sheets_enabled = bool(google_sheets.get("enabled"))
    google_sheets_ready = bool(google_sheets.get("spreadsheet_id") and google_sheets.get("tables"))
    delivery = dict(manifest.get("delivery") or {})
    tracker_states = {
        key: {"configured": True, "available": Path(str(value)).expanduser().is_file(), "path": str(value)}
        for key, value in trackers.items()
    }
    connections = {
        "ringcentral": {
            "ready": ringcentral_export["available"] or ringcentral_api or ringcentral_email_ready,
            "export": ringcentral_export,
            "api_credentials_configured": ringcentral_api,
            "scheduled_email_configured": ringcentral_email_ready,
        },
        "ezlynx": {
            "ready": ezlynx_browser or any(item["available"] for item in ezlynx_exports.values()),
            "browser_secret_references_configured": ezlynx_browser,
            "exports": ezlynx_exports,
        },
        "gmail": {
            "ready": gmail_backend or file_state("email_json")["available"],
            "domain_delegation_configuration_present": gmail_backend,
            "summary_export": file_state("email_json"),
        },
        "appsheet": {
            "enabled": appsheet_enabled,
            "ready": (not appsheet_enabled) or appsheet_ready,
            "credentials_configured": appsheet_ready,
        },
        "google_sheets": {
            "enabled": google_sheets_enabled,
            "ready": (not google_sheets_enabled) or google_sheets_ready,
            "explicit_column_allowlists": all(
                bool((item or {}).get("allowed_columns")) for item in (google_sheets.get("tables") or {}).values()
            ) if google_sheets_enabled else True,
        },
        "delivery": {
            "enabled": bool(delivery.get("enabled")),
            "ready": (not delivery.get("enabled")) or bool(delivery.get("chat_spaces") or delivery.get("email_recipients")),
            "chat_destinations": len(delivery.get("chat_spaces") or []),
            "email_sender_configured": bool(delivery.get("email_sender")),
        },
        "magellan": {
            "ready": file_state("magellan_json")["available"],
            "summary_export": file_state("magellan_json"),
        },
        "trackers": {
            "ready": bool(tracker_states) and all(item["available"] for item in tracker_states.values()),
            "items": tracker_states,
        },
    }
    critical = ("ringcentral", "ezlynx")
    return {
        "ready": all(connections[name]["ready"] for name in critical),
        "manifest": str(path),
        "connections": connections,
        "note": "ready means the report can run with core phone and EZLynx evidence; optional unavailable sources remain UNVERIFIED",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check StreetSmart accountability connections without exposing secrets")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    print(json.dumps(check_connections(args.manifest), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
