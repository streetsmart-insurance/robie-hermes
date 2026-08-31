#!/usr/bin/env python3
"""Atomically enable the already-private Test accountability configuration."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


TEST_ROOT = Path("/opt/streetsmart-hermes-test")
MANIFEST = TEST_ROOT / "accountability" / "connection-manifest.json"
ENV_FILE = Path("/etc/streetsmart-hermes-test/robie-accountability.env")
JOB_DB = TEST_ROOT / "robie-job-engine" / "data" / "jobs.db"
PLACEHOLDERS = {"", "REPLACE_WITH_PRIVATE_CONTROL_CENTER_SHEET_ID", "REPLACE_WITH_PRIVATE_APPSHEET_BACKING_SHEET_ID"}


def _metadata_service_account() -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        value = response.read().decode("utf-8").strip()
    if not value.endswith(".iam.gserviceaccount.com"):
        raise RuntimeError("attached identity is not a service account")
    return value


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _active_employee_emails(snapshot: dict, config: dict) -> list[str]:
    table_name = str(config.get("employee_role_table") or "")
    table = ((snapshot.get("tables") or {}).get(table_name) or {})
    columns = dict(config.get("employee_role_columns") or {})
    email_column = str(columns.get("email") or "Email")
    active_column = str(columns.get("active") or "Active")
    emails: set[str] = set()
    for row in table.get("rows", []) or []:
        active = str(row.get(active_column, "true")).strip().casefold()
        email = str(row.get(email_column) or "").strip().casefold()
        if active not in {"false", "no", "0", "inactive"} and "@" in email:
            emails.add(email)
    return sorted(emails)


def main() -> int:
    if socket.gethostname().split(".", 1)[0] != "hermes-test-01":
        raise SystemExit("refusing every host except hermes-test-01")
    result: dict[str, object] = {"host_is_test": True, "production_touched": False, "delivery_enabled": False}
    if not MANIFEST.is_file() or not JOB_DB.is_file():
        result.update({"configured": False, "reason": "missing_private_manifest_or_job_database"})
        print(json.dumps(result, sort_keys=True))
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    collection = dict(manifest.get("collection") or {})
    ringcentral = dict(collection.get("ringcentral_email") or {})
    scheduled = dict(collection.get("scheduled_reports_email") or {})
    submission = dict(collection.get("ezlynx_submission_browser") or {})
    sheets = dict(manifest.get("google_sheets") or {})
    dashboard = dict(manifest.get("dashboard") or {})
    mailbox = str(ringcentral.get("mailbox") or scheduled.get("mailbox") or "").strip()
    service_account = _metadata_service_account()
    prerequisites = {
        "private_mailbox": "@" in mailbox and not mailbox.endswith("@example.test"),
        "scheduled_report_definitions": bool(scheduled.get("reports")) and bool(
            scheduled.get("allowed_senders") or scheduled.get("allowed_sender_domains")
        ),
        "submission_read_contract": submission.get("read_only") is True
        and submission.get("scope") == "Streetsmart Insurance"
        and submission.get("time_frame") == "All Submissions",
        "employee_sheet_contract": bool(sheets.get("spreadsheet_id") not in PLACEHOLDERS and sheets.get("tables")),
        "dashboard_target": bool(
            dashboard.get("spreadsheet_id") not in PLACEHOLDERS and dashboard.get("sheet_name")
        ),
    }
    result["prerequisites"] = prerequisites
    if not all(prerequisites.values()):
        result.update({"configured": False, "reason": "private_target_configuration_incomplete"})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    from robie_job_engine.google_sheets_accountability import collect_allowlisted_tables
    from robie_job_engine.gmail_accountability import build_keyless_delegated_service
    from robie_job_engine.ringcentral_email_sync import build_keyless_report_mailbox_service
    from robie_job_engine.accountability_dashboard import _client

    snapshot = collect_allowlisted_tables({**sheets, "enabled": True})
    users = _active_employee_emails(snapshot, sheets)
    if not users:
        result.update({"configured": False, "reason": "approved_employee_allowlist_empty"})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    try:
        build_keyless_report_mailbox_service(service_account, mailbox).users().messages().list(
            userId="me", q="newer_than:1d", maxResults=1
        ).execute()
        for user in users:
            build_keyless_delegated_service(service_account, user).users().messages().list(
                userId="me", q="newer_than:1d", maxResults=1
            ).execute()
        _client().spreadsheets().values().get(
            spreadsheetId=str(dashboard["spreadsheet_id"]),
            range=f"'{dashboard['sheet_name']}'!A1:A1",
        ).execute()
    except Exception as exc:
        result.update({"configured": False, "reason": "private_access_check_failed", "error_class": type(exc).__name__})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2

    ringcentral["enabled"] = True
    scheduled["enabled"] = True
    submission["enabled"] = True
    collection.update({
        "ringcentral_email": ringcentral,
        "scheduled_reports_email": scheduled,
        "ezlynx_submission_browser": submission,
    })
    manifest["collection"] = collection
    manifest["google_sheets"] = {**sheets, "enabled": True}
    manifest["dashboard"] = {**dashboard, "enabled": True, "environment_label": "TEST"}
    manifest["delivery"] = {**dict(manifest.get("delivery") or {}), "enabled": False}
    manifest.setdefault("rules", {})["require_complete_evidence"] = True
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = MANIFEST.with_name(f"connection-manifest.pre-accountability-{stamp}.json")
    _atomic_write(backup, MANIFEST.read_text(encoding="utf-8"), 0o600)
    _atomic_write(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n", 0o600)
    env_content = "\n".join((
        f"ROBIE_ACCOUNTABILITY_MANIFEST={MANIFEST}",
        f"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT={service_account}",
        f"ACCOUNTABILITY_REPORT_MAILBOX={mailbox}",
        f"ACCOUNTABILITY_GMAIL_USERS={','.join(users)}",
        "",
    ))
    _atomic_write(ENV_FILE, env_content, 0o600)
    os.environ.update({
        "ROBIE_ENV": "TEST",
        "ROBIE_ACCOUNTABILITY_MANIFEST": str(MANIFEST),
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": service_account,
        "ACCOUNTABILITY_REPORT_MAILBOX": mailbox,
        "ACCOUNTABILITY_GMAIL_USERS": ",".join(users),
    })
    from robie_job_engine.accountability_schedule import install_accountability_schedules
    installed = install_accountability_schedules(str(JOB_DB), str(MANIFEST), timezone_name="America/New_York")
    result.update({
        "configured": True,
        "delegated_mailboxes_verified": len(users) + 1,
        "dashboard_read_verified": True,
        "schedules_verified": len(installed),
        "delivery_enabled": False,
        "backup_created": True,
    })
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
