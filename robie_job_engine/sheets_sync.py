from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .confidence import assess_job_confidence
from .operations import OperationsStore
from .store import JobStore


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def _service():
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _cell(row: list[Any], index: int, default: str = "") -> str:
    return str(row[index]).strip() if index < len(row) and row[index] is not None else default


def _matrix(rows: list[dict[str, Any]], keys: list[str], limit: int) -> list[list[Any]]:
    values = [[item.get(k) if item.get(k) is not None else "" for k in keys] for item in rows[:limit]]
    return values or [[""]]


def _friendly_person(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not captured (older job)"
    if "@" not in raw:
        return raw
    local = raw.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in local.split()) or raw


def _friendly_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        display_timezone = ZoneInfo(os.environ.get("ROBIE_DISPLAY_TIMEZONE", "America/New_York"))
        return stamp.astimezone(display_timezone).strftime("%b %-d, %Y, %-I:%M %p")
    except (ValueError, TypeError):
        return raw


def _friendly_job_name(payload: dict[str, Any], action_type: str, task: str) -> str:
    explicit = (
        payload.get("job_name") or payload.get("workflow_name")
        or payload.get("discussion_title") or payload.get("subject")
    )
    if explicit:
        return str(explicit).strip()
    client = payload.get("client") or payload.get("account_name")
    if client and task:
        return f"{client} — {task}"[:120]
    if action_type == "hermes.google_chat_task":
        return (task or "Google Chat request")[:120]
    return (task or action_type or "ROBIE job")[:120]


def _friendly_jobs(rows: list[dict[str, Any]], limit: int = 1000) -> list[list[Any]]:
    task_names = {
        "ezlynx.reassign": "Reassign an EZLynx account",
        "ezlynx.move_document": "Move an EZLynx document",
        "ezlynx.apply_label": "Apply an EZLynx document label",
        "deployment.smoke": "Deployment safety check",
    }
    status_names = {
        "PENDING": "Queued",
        "RUNNING": "Working now",
        "VERIFYING": "Checking the result",
        "RETRY_WAIT": "Waiting to retry",
        "PAUSED": "Paused — needs attention",
        "UNVERIFIED": "Not verified — needs review",
        "FAILED": "Failed — needs review",
        "COMPLETE": "Complete — independently verified",
    }
    output: list[list[Any]] = []
    for item in rows[:limit]:
        payload = item.get("payload") or {}
        status = str(item.get("status") or "")
        action_type = str(item.get("action_type") or "")
        task = payload.get("task") or payload.get("text") or task_names.get(action_type, action_type)
        requester_raw = (
            payload.get("requested_by") or payload.get("sender") or payload.get("from")
            or "Not captured (older job)"
        )
        requester = _friendly_person(requester_raw)
        account = payload.get("account_id") or payload.get("client") or payload.get("resource_id") or ""
        source = payload.get("source") or (
            "Google Chat" if action_type == "hermes.google_chat_task"
            else "Gmail" if "email" in action_type
            else "Job Engine"
        )
        needs_attention = "YES — review this job" if status in {"UNVERIFIED", "FAILED", "PAUSED"} else "No"
        job_name = _friendly_job_name(payload, action_type, str(task or ""))
        confidence = assess_job_confidence(item)
        recording_url = item.get("recording_drive_url") or ""
        reference_use = "Approved reference" if item.get("recording_reference_approved") else "Not approved for reference"
        output.append([
            _friendly_datetime(item.get("created_at")), job_name, account, requester,
            task, "ROBIE", source, payload.get("skill") or "",
            status_names.get(status, status), f"{confidence.level} — {confidence.score}%",
            "; ".join(confidence.issues), recording_url, reference_use, needs_attention,
            _friendly_datetime(item.get("updated_at")),
            _friendly_datetime(item.get("completed_at")),
            item.get("last_error") or "", item.get("id", ""),
            item.get("attempt_count", 0), item.get("verification_count", 0),
            item.get("input_tokens", 0), item.get("output_tokens", 0),
            item.get("cache_read_tokens", 0), item.get("total_tokens", 0),
            item.get("estimated_cost_usd", 0),
        ])
    return output or [[""]]


def sync(db_path: str, spreadsheet_id: str) -> dict[str, int]:
    api = _service().spreadsheets().values()
    jobs = JobStore(db_path)
    ops = OperationsStore(db_path)
    intake = api.get(spreadsheetId=spreadsheet_id, range="Assignments!A6:P205").execute().get("values", [])
    assignment_updates: list[dict[str, Any]] = []
    imported = 0
    for offset, row in enumerate(intake, start=6):
        task = _cell(row, 4)
        enabled = _cell(row, 10).upper() in {"TRUE", "YES", "1"}
        if not task or not enabled:
            continue
        assignment_id = _cell(row, 0) or str(uuid.uuid4())
        payload = {
            "source": "google_sheet",
            "assignment_id": assignment_id,
            "requested_by": _cell(row, 2),
            "account_id": _cell(row, 3),
            "task": task,
            "skill": _cell(row, 5),
            "priority": _cell(row, 6, "NORMAL"),
            "due_at": _cell(row, 7),
            "attachment_links": [x.strip() for x in _cell(row, 8).split(",") if x.strip()],
            "cadence": _cell(row, 9, "ONCE"),
        }
        job = jobs.create_job("hermes.sheet_assignment", payload, idempotency_key=f"sheet:{assignment_id}")
        imported += 1
        assignment_updates.extend([
            {"range": f"Assignments!A{offset}", "values": [[assignment_id]]},
            {"range": f"Assignments!B{offset}", "values": [[_cell(row, 1) or datetime.now(timezone.utc).isoformat()]]},
            {"range": f"Assignments!L{offset}:P{offset}", "values": [[
                job["id"], str(job["status"]), job["updated_at"],
                "VERIFIED" if str(job["status"]) == "COMPLETE" else "",
                job.get("last_error") or "",
            ]]},
        ])
    data = ops.dashboard_rows()
    writes = [
        {"range": "Dashboard!B9", "values": [[datetime.now(timezone.utc).isoformat()]]},
        {"range": "Jobs!A5:Y5", "values": [[
            "Date Started", "Job Name", "Client / Account", "Requested By",
            "Work Requested", "Owner", "Source", "Skill", "Status",
            "Confidence", "Issues", "Recording", "Reference Use",
            "Needs Attention", "Last Updated", "Date Completed",
            "Last Result / Error", "Job ID", "Attempts", "Verification Checks",
            "Input Tokens", "Output Tokens", "Cache Read Tokens", "Total Tokens",
            "Estimated Model Cost (USD)",
        ]]},
        {"range": "Jobs!A6", "values": _friendly_jobs(data["jobs"])},
        {"range": "Evidence!A6", "values": _matrix(data["evidence"],
            ["job_id","verified","method","source","authoritative","expected_json","observed_json","locator","evidence_sha256","captured_at","created_at"], 1000)},
        {"range": "Schedules!A6", "values": _matrix(data["schedules"],
            ["id","name","action_type","interval_minutes","enabled","next_run_at","last_run_at","last_job_id","updated_at"], 100)},
        {"range": "Artifacts!A6", "values": _matrix(data["artifacts"],
            ["id","job_id","source_platform","original_name","mime_type","size_bytes","sha256","status","destination_ref","created_at","updated_at"], 1000)},
        {"range": "Releases!A6", "values": _matrix(data["releases"],
            ["environment","digest","commit_sha","artifact_uri","status","evidence_json","created_at","updated_at"], 1000)},
        {"range": "Reports!A6", "values": _matrix(data["reports"],
            ["id","report_type","destination","window_start","window_end","status","summary_json","error","created_at","updated_at"], 1000)},
    ] + assignment_updates
    api.batchUpdate(spreadsheetId=spreadsheet_id, body={"valueInputOption": "USER_ENTERED", "data": writes}).execute()
    return {"assignments": imported, "jobs": len(data["jobs"]), "evidence": len(data["evidence"]), "artifacts": len(data["artifacts"]), "recordings": len(data["recordings"]), "releases": len(data["releases"]), "reports": len(data["reports"])}


def sync_from_env(db_path: str) -> dict[str, int] | None:
    sheet_id = os.environ.get("ROBIE_DASHBOARD_SHEET_ID")
    return sync(db_path, sheet_id) if sheet_id else None
