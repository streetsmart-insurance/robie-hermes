#!/usr/bin/env python3
"""Redacted, read-only readiness proof for accountability on hermes-test-01."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path


TEST_ROOT = Path("/opt/streetsmart-hermes-test")
ENV_CANDIDATES = (
    Path("/etc/streetsmart-hermes-test/robie-accountability.env"),
    TEST_ROOT / "accountability" / "robie-accountability.env",
)
MANIFEST_CANDIDATES = (
    TEST_ROOT / "accountability" / "connection-manifest.json",
)


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _error_class(exc: Exception) -> str:
    return type(exc).__name__


def main() -> int:
    if socket.gethostname().split(".", 1)[0] != "hermes-test-01":
        raise SystemExit("refusing every host except hermes-test-01")
    env_path = next((path for path in ENV_CANDIDATES if path.is_file()), None)
    manifest_path = next((path for path in MANIFEST_CANDIDATES if path.is_file()), None)
    env = _load_env(env_path) if env_path else {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path else {}
    service_account = env.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "")
    mailbox = env.get("ACCOUNTABILITY_REPORT_MAILBOX", "")
    users = [value.strip() for value in env.get("ACCOUNTABILITY_GMAIL_USERS", "").split(",") if value.strip()]
    collection = dict(manifest.get("collection") or {})
    dashboard = dict(manifest.get("dashboard") or {})
    result: dict[str, object] = {
        "host_is_test": True,
        "production_paths_used": False,
        "environment_file_present": bool(env_path),
        "manifest_present": bool(manifest_path),
        "delegated_service_account_configured": bool(service_account),
        "report_mailbox_configured": bool(mailbox),
        "employee_mailbox_allowlist_configured": bool(users),
        "scheduled_ringcentral_enabled": bool((collection.get("ringcentral_email") or {}).get("enabled")),
        "scheduled_operational_reports_enabled": bool((collection.get("scheduled_reports_email") or {}).get("enabled")),
        "live_submission_browser_enabled": bool((collection.get("ezlynx_submission_browser") or {}).get("enabled")),
        "dashboard_enabled": bool(dashboard.get("enabled")),
        "delivery_enabled": bool((manifest.get("delivery") or {}).get("enabled")),
        "gmail_report_mailbox_read": False,
        "gmail_employee_metadata_read": False,
        "dashboard_read": False,
    }
    os.environ.update(env)
    if service_account and mailbox:
        try:
            from robie_job_engine.ringcentral_email_sync import build_keyless_report_mailbox_service
            build_keyless_report_mailbox_service(service_account, mailbox).users().messages().list(
                userId="me", q="newer_than:1d", maxResults=1
            ).execute()
            result["gmail_report_mailbox_read"] = True
        except Exception as exc:  # report class only; never log mailbox or response body
            result["gmail_report_mailbox_error_class"] = _error_class(exc)
    if service_account and users:
        try:
            from robie_job_engine.gmail_accountability import build_keyless_delegated_service
            build_keyless_delegated_service(service_account, users[0]).users().messages().list(
                userId="me", q="newer_than:1d", maxResults=1
            ).execute()
            result["gmail_employee_metadata_read"] = True
        except Exception as exc:
            result["gmail_employee_metadata_error_class"] = _error_class(exc)
    if dashboard.get("enabled") and dashboard.get("spreadsheet_id") and dashboard.get("sheet_name"):
        try:
            from robie_job_engine.accountability_dashboard import _client
            _client().spreadsheets().values().get(
                spreadsheetId=str(dashboard["spreadsheet_id"]),
                range=f"'{dashboard['sheet_name']}'!A1:A1",
            ).execute()
            result["dashboard_read"] = True
        except Exception as exc:
            result["dashboard_error_class"] = _error_class(exc)
    ready = all((
        result["environment_file_present"],
        result["manifest_present"],
        result["delegated_service_account_configured"],
        result["report_mailbox_configured"],
        result["employee_mailbox_allowlist_configured"],
        result["gmail_report_mailbox_read"],
        result["gmail_employee_metadata_read"],
        result["dashboard_read"],
    )) and not result["delivery_enabled"]
    result["ready"] = ready
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
