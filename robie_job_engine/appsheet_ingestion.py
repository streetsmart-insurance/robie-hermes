"""AppSheet workstream ingestion adapter for policy changes, audits, mortgagee verifications, and manual renewals."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .operational_trackers import (
    TrackerException,
    TRACKER_DEFINITIONS,
    _blocker_party,
    _classify_tracker_department,
    _detect_carrier_doc_status,
    _detect_match_status,
    _next_action,
    _normalize_tracker_department,
    _value,
    _CLOSED,
)
from .center_audits import parse_date


WORKSTREAM_TABLE_ALIASES: dict[str, str] = {
    "policychanges": "policy_changes",
    "policychange": "policy_changes",
    "policychangerequests": "policy_changes",
    "changerequests": "policy_changes",
    "changes": "policy_changes",
    "audits": "audits",
    "audit": "audits",
    "premiumaudits": "audits",
    "premiumaudit": "audits",
    "policytransactions": "audits",
    "policytransactionsauditcancellation": "audits",
    "mortgageeverifications": "mortgagee_verifications",
    "mortgageeverification": "mortgagee_verifications",
    "mortgagee": "mortgagee_verifications",
    "mortgagees": "mortgagee_verifications",
    "lenderverifications": "mortgagee_verifications",
    "mortgageeintake": "mortgagee_verifications",
    "manualrenewals": "manual_renewals",
    "manualrenewal": "manual_renewals",
    "renewals": "manual_renewals",
    "expirations": "manual_renewals",
    "renewalremarketing": "manual_renewals",
}


def normalize_appsheet_table_name(name: str) -> Optional[str]:
    cleaned = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    if not cleaned:
        return None
    if cleaned in WORKSTREAM_TABLE_ALIASES:
        return WORKSTREAM_TABLE_ALIASES[cleaned]
    if "mortgagee" in cleaned or "lender" in cleaned:
        return "mortgagee_verifications"
    if "audit" in cleaned:
        return "audits"
    if "renewal" in cleaned or "expiration" in cleaned or "remarket" in cleaned:
        return "manual_renewals"
    if "policychange" in cleaned or "changerequest" in cleaned or "endorsement" in cleaned or "changes" in cleaned:
        return "policy_changes"
    return None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.-]", "", str(value).strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_appsheet_row_to_exception(
    row: Mapping[str, Any],
    workstream: str,
    row_number: int = 0,
    *,
    source_row: Optional[int] = None,
    as_of: date,
    employee_departments: Optional[Mapping[str, str]] = None,
) -> Optional[TrackerException]:
    """Convert an AppSheet table row into a normalized TrackerException."""
    if not any(str(v or "").strip() for v in row.values()):
        return None

    effective_row = source_row if source_row is not None else row_number
    definition = TRACKER_DEFINITIONS.get(workstream) or TRACKER_DEFINITIONS.get("policy_changes")

    status = (
        _value(row, definition.status_columns)
        or _value(row, ("Status", "State", "Disposition", "Audit Status", "Verification Status", "Renewal Status"))
        or "Unknown"
    )
    if status.lower().strip() in _CLOSED:
        return None

    opened = parse_date(
        _value(row, definition.opened_columns)
        or _value(
            row,
            (
                "Date",
                "Created Date",
                "Created_Date",
                "Request Date",
                "Request_Date",
                "Date Requested",
                "Date_Requested",
                "Received Date",
                "Received_Date",
                "Date Received",
                "Date_Received",
                "Transaction Date",
                "Transaction_Date",
            ),
        )
    )
    due = parse_date(
        _value(row, definition.due_columns)
        or _value(
            row,
            (
                "Due Date",
                "Due_Date",
                "Closing Date",
                "Closing_Date",
                "Renewal Date",
                "Renewal_Date",
                "Expiration Date",
                "Expiration_Date",
                "Needed By",
                "Effective Date",
                "Effective_Date",
            ),
        )
    )
    age_days = max(0, (as_of - opened).days) if opened else None

    notes = _value(row, definition.note_columns) or _value(row, ("Notes", "Remarks", "Description", "Details", "Comments"))
    blocker = _blocker_party(status, notes)
    owner = (
        _value(row, definition.owner_columns)
        or _value(
            row,
            (
                "Assigned To",
                "Assigned_To",
                "Owner",
                "CSR",
                "Producer",
                "Agent",
                "Assigned Agent",
                "Assigned_Agent",
            ),
        )
        or "Unassigned"
    )
    account = (
        _value(row, definition.account_columns)
        or _value(
            row,
            (
                "Account",
                "Account Name",
                "Insured",
                "Named Insured",
                "Named_Insured",
                "Customer",
                "Client Name",
                "Client",
                "Borrower",
                "Applicant",
            ),
        )
        or "Unknown account"
    )

    ezlynx_reference = _value(
        row,
        ("EZLynx Account ID", "EZLynx URL", "Applicant ID", "Account ID", "Policy Number", "Policy No.", "Link"),
    ) or "UNVERIFIED"

    lob = _value(row, ("Line of Business", "LOB", "Policy Type", "Type", "Coverage")) or "UNVERIFIED"
    change_desc = _value(row, ("Change Description", "Description", "Change Details", "Request Type", "Transaction Type", "Subject"))
    raw_doc_status = _value(row, ("Carrier Document", "Carrier Doc", "Carrier_Doc", "Carrier Status", "Document Status", "Policy Dec Status", "Carrier Retrieval", "Retrieval Status"))
    raw_match = _value(row, ("3-Way Match", "Match Status", "Match_Status", "Verification Status", "Match", "Verification"))

    # Workstream-specific attributes
    audit_type = ""
    premium_amount = None
    lender_name = ""
    loan_number = ""
    renewal_date = None
    rate_change_pct = None

    if workstream == "audits":
        audit_type = _value(row, ("Audit Type", "Audit_Type", "Type of Audit", "Audit Method", "Transaction Type")) or lob
        premium_amount = _parse_float(_value(row, ("Additional Premium", "Return Premium", "Audit Amount", "Premium Difference", "Amount")))
    elif workstream == "mortgagee_verifications":
        lender_name = _value(row, ("Mortgagee", "Mortgagee Name", "Lender", "Lender Name", "Lender_Name", "Escrow", "Bank"))
        loan_number = _value(row, ("Loan Number", "Loan #", "Loan No", "Loan_Number", "Loan", "Account #", "Escrow #"))
    elif workstream == "manual_renewals":
        renewal_date = _value(row, ("Renewal Date", "Expiration Date", "Renewal_Date", "X-Date", "Effective Date"))
        rate_change_pct = _parse_float(_value(row, ("Rate Change", "Rate Change Pct", "Rate_Change_Pct", "Rate Change %", "Rate Increase %", "Increase %", "Rate Increase", "Pct Change")))
        premium_amount = _parse_float(_value(row, ("Renewal Premium", "Renewal_Premium", "Expiring Premium", "Premium", "Amount")))

    effective_lob = lob if lob != "UNVERIFIED" else (audit_type or "UNVERIFIED")
    effective_desc = change_desc or audit_type or lender_name
    dept = _classify_tracker_department(row, definition, owner, notes, effective_lob, effective_desc, employee_departments)
    carrier_doc_status = _detect_carrier_doc_status(raw_doc_status, status, notes, blocker)
    match_status = _detect_match_status(raw_match, carrier_doc_status, status, notes)

    reasons: list[str] = []
    threshold = definition.aging_days if definition else 7
    if age_days is not None and age_days > threshold:
        reasons.append(f"open for {age_days} days (threshold {threshold})")
    if due and due < as_of:
        reasons.append(f"due date passed by {(as_of - due).days} days")
    if status == "Unknown":
        reasons.append("status missing")

    severity = "high" if due and due < as_of else ("medium" if (age_days or 0) > threshold else "low")
    if not reasons:
        reasons.append(f"open item in {definition.display_name if definition else workstream}")

    return TrackerException(
        tracker_key=workstream,
        tracker_name=definition.display_name if definition else workstream.replace("_", " ").title(),
        department=dept,
        account=account,
        owner=owner,
        status=status,
        opened_date=opened.isoformat() if opened else None,
        age_days=age_days,
        due_date=due.isoformat() if due else None,
        severity=severity,
        reasons=tuple(dict.fromkeys(reasons)),
        source_row_number=effective_row,
        blocker_party=blocker,
        next_action=_next_action(definition, blocker),
        notification_target=owner,
        ezlynx_reference=ezlynx_reference,
        lob=lob,
        carrier_doc_status=carrier_doc_status,
        match_status=match_status,
        change_description=change_desc,
        workstream=workstream,
        audit_type=audit_type,
        premium_amount=premium_amount,
        lender_name=lender_name,
        loan_number=loan_number,
        renewal_date=renewal_date,
        rate_change_pct=rate_change_pct,
    )


def ingest_appsheet_workstreams(
    appsheet_data: Mapping[str, Any] | str | Path,
    *,
    as_of: Optional[date] = None,
    employee_departments: Optional[Mapping[str, str]] = None,
    by_workstream: bool = False,
    as_dicts: bool = False,
) -> dict[str, Any] | list[Any]:
    """
    Ingest AppSheet data into normalized TrackerException collections by workstream.
    
    Accepts:
      - Path to JSON file
      - JSON string
      - Parsed dict with "tables" mapping or direct table mapping
    """
    target_date = as_of or datetime.now().date()
    payload: dict[str, Any]

    if isinstance(appsheet_data, (Path, str)):
        raw_str = Path(appsheet_data).read_text(encoding="utf-8") if isinstance(appsheet_data, Path) or Path(str(appsheet_data)).exists() else str(appsheet_data)
        try:
            payload = json.loads(raw_str)
        except Exception:
            return {} if by_workstream else []
    else:
        payload = dict(appsheet_data)

    tables: dict[str, Any] = {}
    if "tables" in payload and isinstance(payload["tables"], dict):
        tables = payload["tables"]
    else:
        tables = payload

    findings_by_workstream: dict[str, list[TrackerException]] = {
        "policy_changes": [],
        "audits": [],
        "mortgagee_verifications": [],
        "manual_renewals": [],
    }

    for raw_table_name, table_content in tables.items():
        canonical_stream = normalize_appsheet_table_name(raw_table_name)
        if not canonical_stream:
            continue

        rows: Sequence[Mapping[str, Any]] = []
        if isinstance(table_content, dict) and "rows" in table_content and isinstance(table_content["rows"], list):
            rows = table_content["rows"]
        elif isinstance(table_content, list):
            rows = table_content

        for idx, row in enumerate(rows, start=2):
            if not isinstance(row, dict):
                continue
            exc = parse_appsheet_row_to_exception(
                row,
                canonical_stream,
                idx,
                as_of=target_date,
                employee_departments=employee_departments,
            )
            if exc:
                findings_by_workstream[canonical_stream].append(exc)

    if by_workstream:
        if as_dicts:
            return {
                ws: [asdict(e) for e in exceptions]
                for ws, exceptions in findings_by_workstream.items()
            }
        return findings_by_workstream

    flat_list: list[Any] = []
    for ws in ("policy_changes", "audits", "mortgagee_verifications", "manual_renewals"):
        flat_list.extend(findings_by_workstream.get(ws, []))

    if as_dicts:
        return [asdict(e) for e in flat_list]
    return flat_list
