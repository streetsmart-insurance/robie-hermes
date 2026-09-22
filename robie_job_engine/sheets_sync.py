from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .confidence import assess_job_confidence
from .operations import OperationsStore
from .store import JobStore


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
DRIVE_SCOPES = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
}


def _service():
    import google.auth
    from googleapiclient.discovery import build
    token_file = os.environ.get("ROBIE_GOOGLE_TOKEN_FILE", "").strip()
    if token_file:
        from google.oauth2.credentials import Credentials
        # Preserve the OAuth grant exactly as issued. Supplying a different
        # scope list during refresh can produce invalid_scope even when the
        # stored token already has broader Drive access.
        creds = Credentials.from_authorized_user_file(token_file)
        granted = set(creds.scopes or ())
        if SCOPES[0] not in granted or not (granted & DRIVE_SCOPES):
            raise PermissionError("Google token lacks required Sheets/Drive scopes")
    else:
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
        "carrier.proposal": "Create a carrier proposal",
        "browser.read": "Read a page without changing it",
        "hermes.plain_english": "Plain-English request",
        "deployment.smoke": "Deployment safety check",
        "ascend.locator_artifact_audit": "Ascend locator and artifact audit",
    }
    status_names = {
        "PENDING": "Queued",
        "NEEDS_SKILL": "Needs a Skill — waiting",
        "NEEDS_CLARIFICATION": "Needs clarification — waiting",
        "NEEDS_AUTH": "Needs authorization — waiting",
        "AWAITING_HUMAN_INPUT": "Waiting on you — needs your input",
        "WAITING": "Waiting — not complete",
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
        needs_attention = "YES — review this job" if status in {
            "UNVERIFIED", "FAILED", "PAUSED", "WAITING",
            "NEEDS_CLARIFICATION", "NEEDS_AUTH", "NEEDS_SKILL",
        } else "No"
        job_name = _friendly_job_name(payload, action_type, str(task or ""))
        confidence = assess_job_confidence(item)
        recording_links = item.get("recording_links") or []
        if recording_links:
            recording_url = "\n".join(
                f"Segment {entry['segment']}: {entry['url']}" for entry in recording_links
            )
        elif item.get("recording_status") == "FAILED":
            recording_url = (
                "Recording upload failed"
                if item.get("recording_failure_stage") == "UPLOAD"
                else "Recording failed"
            )
        elif item.get("recording_exemption"):
            reason = str((item.get("recording_exemption") or {}).get("reason") or "").strip()
            recording_url = "Recording exempt — sensitive authentication flow"
            if reason:
                recording_url = f"{recording_url}: {reason}"
        else:
            recording_url = ""
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


_ERROR_PREFIX_RE = re.compile(
    r"^(?:[A-Za-z_][\w.]*Error|Exception|TimeoutError)\s*:\s*", re.IGNORECASE
)
_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r'"?(?:/[\w.\-]+)+/?"?')


def _clean_error(raw: Any) -> str:
    """Return a reader-safe one-line summary of an engine error.

    Drops tracebacks, URLs, and file paths so the value is safe to show a
    non-technical reader in the Control Center.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # In a traceback the last line carries the actual error message.
    line = lines[-1] if lines[0].lower().startswith("traceback") else lines[0]
    line = _URL_RE.sub("", line)
    line = _PATH_RE.sub("", line)
    line = _ERROR_PREFIX_RE.sub("", line)
    line = re.sub(r"\s{2,}", " ", line).strip(" :;,-")
    return line[:220].strip()


def _plain_english_what_went_wrong(item: dict[str, Any]) -> str:
    """Plain-English explanation of why a job is stuck or failed.

    Returns 1-3 short sentences with no jargon, tracebacks, or file paths.
    Jobs in good or neutral states return an empty string.
    """
    status = str(item.get("status") or "").strip().upper()
    payload = item.get("payload") or {}
    error = _clean_error(item.get("last_error"))
    requester = _friendly_person(
        payload.get("requested_by") or payload.get("sender") or payload.get("from") or ""
    )
    who = requester if requester else "the person who requested it"
    skill = str(payload.get("skill") or "").strip()

    if status == "FAILED":
        detail = f" What the system reported: {error}." if error else ""
        return (
            "This job failed and will not try again on its own."
            f"{detail} Please review it and re-run the job or fix the underlying problem."
        )
    if status == "NEEDS_AUTH":
        what = error or "an approval this job cannot grant itself"
        return (
            f"This job is waiting for authorization: {what}."
            f" {who} needs to approve it before it can continue."
        )
    if status in {"NEEDS_CLARIFICATION", "AWAITING_HUMAN_INPUT"}:
        need = error or "a missing detail"
        return (
            f"This job is waiting on {who} for: {need}."
            " Once they reply with the answer, it will continue on its own."
        )
    if status == "UNVERIFIED":
        what = error or "the result in the destination system"
        return (
            "The work ran, but the result could not be independently verified:"
            f" {what}. Please check it manually before treating it as done."
        )
    if status == "PAUSED":
        why = error or "it was paused by a person or a safety check"
        return f"This job is paused: {why}. Unpause it when you are ready for it to continue."
    if status == "NEEDS_SKILL":
        what = skill or error or "a capability that is not installed yet"
        return (
            f"This job is stuck because it needs a skill that is not set up yet: {what}."
            " Ask the team to add it, then the job can continue."
        )
    return ""


def _runtime_job_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Return the authoritative values for the Control Center runtime columns."""
    status = str(item.get("status") or "")
    current_step = {
        "COMPLETE": "Completed and independently verified",
        "UNVERIFIED": "Destination state was not independently verified",
        "FAILED": "Execution failed",
        "NEEDS_AUTH": "Waiting for authorization",
        "VERIFYING": "Independently verifying destination result",
        "RUNNING": "Executing bounded work",
        "PENDING": "Queued for execution",
    }.get(status, status.replace("_", " ").title())
    checks = int(item.get("verification_count") or 0)
    verified = int(item.get("verified_evidence_count") or 0)
    authoritative = int(item.get("authoritative_evidence_count") or 0)
    grade_label = item.get("grade_label")
    verification_status = (
        grade_label if grade_label
        else "Verified" if status == "COMPLETE" and verified > 0 and authoritative > 0
        else "Needs review" if status in {"UNVERIFIED", "FAILED"}
        else "Pending"
    )
    return {
        "current_step": current_step,
        "step_progress": f"{checks} verification check{'s' if checks != 1 else ''}",
        "last_activity": _friendly_datetime(item.get("updated_at")),
        "verification_status": verification_status,
        "evidence_count": verified,
        "control_mode": "ROBIE",
    }


def upsert_job_rows(
    db_path: str,
    spreadsheet_id: str,
    job_ids: list[str] | tuple[str, ...] | set[str],
) -> dict[str, int]:
    """Update only the requested Jobs rows, preserving every unrelated ledger row."""
    requested = {str(job_id).strip() for job_id in job_ids if str(job_id).strip()}
    if not requested:
        return {"jobs": 0, "updated": 0, "appended": 0}

    artifact_root = os.environ.get("ROBIE_ARTIFACT_ROOT") or str(
        Path(db_path).resolve().parent / "artifacts"
    )
    data = OperationsStore(db_path, artifact_root=artifact_root).dashboard_rows()
    selected = [item for item in data["jobs"] if str(item.get("id")) in requested]
    found = {str(item.get("id")) for item in selected}
    missing = sorted(requested - found)
    if missing:
        raise ValueError(f"job IDs were not found in the local ledger: {', '.join(missing)}")

    api = _service().spreadsheets().values()
    existing = api.get(
        spreadsheetId=spreadsheet_id,
        range="Jobs!A6:Y",
    ).execute().get("values", [])
    row_by_job_id = {
        _cell(row, 17): sheet_row
        for sheet_row, row in enumerate(existing, start=6)
        if _cell(row, 17)
    }
    next_row = 6 + len(existing)
    writes: list[dict[str, Any]] = []
    updated = 0
    appended = 0
    for row in _friendly_jobs(selected, limit=len(selected)):
        job_id = _cell(row, 17)
        sheet_row = row_by_job_id.get(job_id)
        if sheet_row is None:
            sheet_row = next_row
            next_row += 1
            appended += 1
        else:
            updated += 1
        job = next(item for item in selected if str(item.get("id")) == job_id)
        runtime = _runtime_job_fields(job)
        writes.extend([
            {"range": f"Jobs!A{sheet_row}:Y{sheet_row}", "values": [row]},
            {"range": f"Jobs!Z{sheet_row}:AB{sheet_row}", "values": [[
                runtime["current_step"], runtime["step_progress"], runtime["last_activity"],
            ]]},
            {"range": f"Jobs!AD{sheet_row}:AE{sheet_row}", "values": [[
                runtime["verification_status"], runtime["evidence_count"],
            ]]},
            {"range": f"Jobs!AG{sheet_row}", "values": [[runtime["control_mode"]]]},
            {"range": f"Jobs!AH{sheet_row}", "values": [[_plain_english_what_went_wrong(job)]]},
        ])

    if writes:
        api.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": writes},
        ).execute()
    return {"jobs": len(selected), "updated": updated, "appended": appended}


def _truthy_cell(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {"true", "yes", "1"}


def publish_job_to_control_center(
    db_path: str,
    spreadsheet_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Upsert one Job plus its evidence and verify the exact ledger read-back.

    Publication is intentionally targeted: unrelated Jobs and Evidence rows are
    never cleared or reordered.  A caller may advertise COMPLETE only after
    this function confirms the exact Job ID, terminal status, authoritative
    evidence, and every READY recording segment in the Control Center.
    """
    job_id = str(job_id).strip()
    if not job_id:
        raise ValueError("job_id is required")
    artifact_root = os.environ.get("ROBIE_ARTIFACT_ROOT") or str(
        Path(db_path).resolve().parent / "artifacts"
    )
    dashboard = OperationsStore(db_path, artifact_root=artifact_root).dashboard_rows()
    selected = [item for item in dashboard["jobs"] if str(item.get("id")) == job_id]
    if len(selected) != 1:
        raise ValueError(f"exact Job {job_id} was not found in the local ledger")
    job = selected[0]
    expected_row = _friendly_jobs(selected, limit=1)[0]
    expected_status = _cell(expected_row, 8)
    expected_recording = _cell(expected_row, 11)
    if str(job.get("status")) == "COMPLETE":
        if int(job.get("authoritative_evidence_count") or 0) < 1:
            raise RuntimeError("COMPLETE publication requires authoritative evidence")
        if not job.get("recording_links") and not job.get("recording_exemption"):
            raise RuntimeError("COMPLETE publication requires a READY recording link or documented exemption")

    result = upsert_job_rows(db_path, spreadsheet_id, [job_id])
    api = _service().spreadsheets().values()

    evidence = [
        item for item in dashboard.get("evidence", [])
        if str(item.get("job_id")) == job_id
    ]
    existing_evidence = api.get(
        spreadsheetId=spreadsheet_id,
        range="Evidence!A6:K",
    ).execute().get("values", [])
    row_by_digest = {
        _cell(row, 8): sheet_row
        for sheet_row, row in enumerate(existing_evidence, start=6)
        if _cell(row, 8)
    }
    next_evidence_row = 6 + len(existing_evidence)
    evidence_writes: list[dict[str, Any]] = []
    for item in evidence:
        digest = str(item.get("evidence_sha256") or "")
        sheet_row = row_by_digest.get(digest)
        if sheet_row is None:
            sheet_row = next_evidence_row
            next_evidence_row += 1
        values = [[
            item.get(key) if item.get(key) is not None else ""
            for key in (
                "job_id", "verified", "method", "source", "authoritative",
                "expected_json", "observed_json", "locator", "evidence_sha256",
                "captured_at", "created_at",
            )
        ]]
        evidence_writes.append(
            {"range": f"Evidence!A{sheet_row}:K{sheet_row}", "values": values}
        )
    if evidence_writes:
        api.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": evidence_writes},
        ).execute()

    job_rows = api.get(
        spreadsheetId=spreadsheet_id,
        range="Jobs!A6:Y",
    ).execute().get("values", [])
    matching = [
        (sheet_row, row)
        for sheet_row, row in enumerate(job_rows, start=6)
        if _cell(row, 17) == job_id
    ]
    if len(matching) != 1:
        raise RuntimeError(
            f"Control Center read-back found {len(matching)} rows for Job {job_id}"
        )
    sheet_row, observed_row = matching[0]
    if _cell(observed_row, 8) != expected_status:
        raise RuntimeError("Control Center status read-back did not match")
    if _cell(observed_row, 11) != expected_recording:
        raise RuntimeError("Control Center recording read-back did not match")

    evidence_rows = api.get(
        spreadsheetId=spreadsheet_id,
        range="Evidence!A6:K",
    ).execute().get("values", [])
    authoritative = [
        row for row in evidence_rows
        if _cell(row, 0) == job_id
        and _truthy_cell(row[1] if len(row) > 1 else None)
        and _truthy_cell(row[4] if len(row) > 4 else None)
    ]
    if str(job.get("status")) == "COMPLETE" and not authoritative:
        raise RuntimeError("Control Center authoritative evidence read-back is missing")
    return {
        **result,
        "job_id": job_id,
        "sheet_row": sheet_row,
        "evidence_rows": len(authoritative),
        "recording": expected_recording,
        "status": expected_status,
    }


def sync(db_path: str, spreadsheet_id: str) -> dict[str, int]:
    spreadsheets = _service().spreadsheets()
    api = spreadsheets.values()
    metadata = spreadsheets.get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties.title",
    ).execute()
    available_sheets = {
        str(sheet.get("properties", {}).get("title") or "").strip()
        for sheet in metadata.get("sheets", [])
    }
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
    # The Control Center can intentionally omit optional operational tabs.
    # Google rejects an entire values.batchUpdate if any one range names a
    # missing sheet, so only send writes whose destination is present. This
    # keeps the core Jobs/Recording ledger current without recreating or
    # requiring display-only tabs such as Releases or Reports.
    writes = [
        write for write in writes
        if str(write["range"]).split("!", 1)[0] in available_sheets
    ]
    api.batchUpdate(spreadsheetId=spreadsheet_id, body={"valueInputOption": "USER_ENTERED", "data": writes}).execute()
    return {"assignments": imported, "jobs": len(data["jobs"]), "evidence": len(data["evidence"]), "artifacts": len(data["artifacts"]), "recordings": len(data["recordings"]), "releases": len(data["releases"]), "reports": len(data["reports"])}


def sync_from_env(db_path: str) -> dict[str, int] | None:
    sheet_id = os.environ.get("ROBIE_DASHBOARD_SHEET_ID")
    return sync(db_path, sheet_id) if sheet_id else None
