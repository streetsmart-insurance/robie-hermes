"""The Confirmations tab on the Operations Control Center board.

This is the human-facing side of the HITL plan-confirmation gate: pending
plan drafts appear as plain-English rows, and the human approves or rejects
by typing APPROVE or REJECT in the "Your decision" column. The next sync
ingests that decision into the ``plan_confirmations`` ledger via
``confirmations.approve`` / ``confirmations.reject`` (which fail closed on
anything that is not PENDING) and writes the decided status back.

Ordering matters: decisions are ingested BEFORE the tab is rewritten, so a
sync never wipes a decision the human just typed. When rewriting, any
decision text still sitting on a row whose record is still PENDING is
carried over, so a decision typed between ingest and rewrite survives to
the next sync.

Column layout (row 1 = headers, data from row 2):
  A Confirmation ID
  B What needs approval      (one plain-English line)
  C Job type
  D Details                  (proposed changes, plain English)
  E Status
  F Requested by
  G Requested at
  H Your decision            (human types APPROVE or REJECT here)
  I Reason                   (optional; used for REJECT)
  J Decided by
  K Decided at
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from . import confirmations
from .store import JobStore


TAB_TITLE = "Confirmations"
DATA_START_ROW = 2

HEADERS = [
    "Confirmation ID",
    "What needs approval",
    "Job type",
    "Details",
    "Status",
    "Requested by",
    "Requested at",
    "Your decision (type APPROVE or REJECT)",
    "Reason (optional)",
    "Decided by",
    "Decided at",
]

# Column indexes (0-based) into a sheet row.
(ID, SUMMARY, JOB_TYPE, DETAILS, STATUS, REQUESTED_BY, REQUESTED_AT,
 DECISION, REASON, DECIDED_BY, DECIDED_AT) = range(len(HEADERS))

DEFAULT_DECIDER = "Carlo Ferrara"
APPROVE_WORD = "APPROVE"
REJECT_WORD = "REJECT"

_JOB_TYPE_LABELS = {
    "policy_change": "Policy change",
    "carrier_quote": "Carrier quote",
    "carrier_call": "Carrier call",
}


def _cell(row: list[Any], index: int) -> str:
    return str(row[index]).strip() if index < len(row) and row[index] is not None else ""


def _friendly_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        tz = ZoneInfo("America/New_York")
        return stamp.astimezone(tz).strftime("%b %-d, %Y, %-I:%M %p")
    except (ValueError, TypeError):
        return text


def _details_text(record: Mapping[str, Any]) -> str:
    """Proposed changes as plain English, capped for a sheet cell."""
    changes = confirmations._parse_changes(record)
    policy = str(changes.get("policy_number") or "").strip()
    change_map = changes.get("changes")
    spans = changes.get("field_spans")
    if not isinstance(spans, Mapping):
        spans = {}
    parts: list[str] = []
    if isinstance(change_map, Mapping):
        for field, value in change_map.items():
            if isinstance(value, Mapping):
                old = value.get("old", value.get("from"))
                new = value.get("new", value.get("to", value.get("value")))
                if old is not None and new is not None:
                    parts.append(f"{field}: {old} -> {new}")
                elif new is not None:
                    parts.append(f"{field}: {new}")
                else:
                    parts.append(f"{field}: {json.dumps(value, default=str)}")
            else:
                parts.append(f"{field}: {value}")
            # Provenance, not just the value: the quote + source for the field.
            raw_span = spans.get(field)
            if isinstance(raw_span, Mapping):
                source = str(raw_span.get("source_id") or "").strip()
                quote = str(raw_span.get("quote") or "").strip()
                if source or quote:
                    snippet = (quote[:70] + "...") if len(quote) > 70 else quote
                    parts[-1] += f' [source: {source or "unknown"}, "{snippet}"]'
            elif raw_span:
                parts[-1] += f' [source: "{str(raw_span)[:70]}"]'
    elif isinstance(change_map, list):
        for entry in change_map:
            parts.append(str(entry)[:120])
    detail = "; ".join(parts)[:900]
    if policy:
        detail = f"Policy {policy}. {detail}" if detail else f"Policy {policy}."
    return detail or str(record.get("draft_summary") or "")


def _record_row(record: Mapping[str, Any]) -> list[str]:
    return [
        str(record.get("id") or ""),
        confirmations.confirmation_summary(record),
        _JOB_TYPE_LABELS.get(str(record.get("job_type") or ""), str(record.get("job_type") or "")),
        _details_text(record),
        str(record.get("status") or ""),
        str(record.get("requested_by") or ""),
        _friendly_datetime(record.get("created_at")),
        "",  # Your decision: filled by the human
        str(record.get("decision_reason") or ""),
        str(record.get("decided_by") or ""),
        _friendly_datetime(record.get("decided_at")),
    ]


def _values_api(provided: Any = None) -> Any:
    if provided is not None:
        return provided
    from . import sheets_sync
    return sheets_sync._service().spreadsheets().values()


def ensure_tab(spreadsheet_id: str, sheets_api: Any = None) -> bool:
    """Create the Confirmations tab if it is missing. Returns True if created."""
    if sheets_api is None:
        from . import sheets_sync
        sheets_api = sheets_sync._service().spreadsheets()
    meta = sheets_api.get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties.title"
    ).execute()
    titles = {
        str(s.get("properties", {}).get("title") or "")
        for s in meta.get("sheets", [])
    }
    if TAB_TITLE in titles:
        return False
    sheets_api.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": TAB_TITLE}}}]},
    ).execute()
    return True


def read_tab_rows(api: Any, spreadsheet_id: str) -> list[list[str]]:
    """Raw rows currently on the tab (headers + data), as strings."""
    resp = api.get(
        spreadsheetId=spreadsheet_id,
        range=f"{TAB_TITLE}!A1:K",
    ).execute()
    return [[_cell(row, i) for i in range(len(HEADERS))] for row in resp.get("values", [])]


def apply_decisions_from_sheet(
    db_path: str,
    spreadsheet_id: str,
    values_api: Any = None,
) -> list[dict[str, Any]]:
    """Ingest APPROVE/REJECT decisions typed on the tab.

    Only rows whose ledger record is still PENDING are decided; everything
    else is skipped. Returns one dict per applied decision.
    """
    api = _values_api(values_api)
    rows = read_tab_rows(api, spreadsheet_id)
    store = JobStore(db_path)
    applied: list[dict[str, Any]] = []
    for row in rows[1:]:  # skip headers
        confirmation_id = _cell(row, ID)
        decision = _cell(row, DECISION).upper()
        if not confirmation_id or decision not in (APPROVE_WORD, REJECT_WORD):
            continue
        record = confirmations.get(confirmation_id, store=store)
        if record is None:
            continue  # unknown id on the sheet: never invent a record
        if str(record.get("status")) != "PENDING":
            continue  # already decided: the write-back owns this row now
        decided_by = _cell(row, DECIDED_BY) or DEFAULT_DECIDER
        reason = _cell(row, REASON)
        if decision == APPROVE_WORD:
            updated = confirmations.approve(confirmation_id, decided_by, store=store)
        else:
            updated = confirmations.reject(confirmation_id, decided_by, reason, store=store)
        applied.append({
            "confirmation_id": confirmation_id,
            "decision": decision,
            "decided_by": decided_by,
            "status": updated.get("status"),
        })
    return applied


def sync_confirmations(
    db_path: str,
    spreadsheet_id: str,
    values_api: Any = None,
    sheets_api: Any = None,
    notify: bool = False,
    chat_poster: Any = None,
    gmail_sender: Any = None,
    chat_thread_poster: Any = None,
    zap_trigger: Any = None,
) -> dict[str, Any]:
    """Full sync of the Confirmations tab. Ingest first, then rewrite.

    1. Expire stale PENDING confirmations (72h).
    2. Ingest APPROVE/REJECT decisions typed on the tab.
    3. Rewrite the tab (headers + one row per record, pending first),
       carrying over any decision text still sitting on a still-PENDING row.
    4. Read the tab back and verify the write landed.
    5. When ``notify`` is true, fan out Chat + email for PENDING records
       Carlo has not been told about yet (idempotent; failures are
       returned, never raised).
    """
    store = JobStore(db_path)
    expired = confirmations.expire_old(store)

    api = _values_api(values_api)
    ensure_tab(spreadsheet_id, sheets_api=sheets_api)

    applied = apply_decisions_from_sheet(db_path, spreadsheet_id, values_api=api)

    # Carry over decision text the human typed on rows that are still PENDING
    # (e.g. typed between a previous rewrite and this sync's ingest).
    pending_decisions: dict[str, tuple[str, str]] = {}
    for row in read_tab_rows(api, spreadsheet_id)[1:]:
        cid = _cell(row, ID)
        if not cid:
            continue
        record = confirmations.get(cid, store=store)
        if record is not None and str(record.get("status")) == "PENDING":
            decision = _cell(row, DECISION)
            reason = _cell(row, REASON)
            if decision:
                pending_decisions[cid] = (decision, reason)

    conn = store.connect()
    try:
        db_rows = conn.execute(
            """SELECT * FROM plan_confirmations
               ORDER BY CASE WHEN status = 'PENDING' THEN 0 ELSE 1 END,
                        CASE WHEN status = 'PENDING' THEN created_at END ASC,
                        decided_at DESC"""
        ).fetchall()
        records = [dict(r) for r in db_rows]
    finally:
        conn.close()

    values: list[list[str]] = [list(HEADERS)]
    for record in records:
        sheet_row = _record_row(record)
        carried = pending_decisions.get(str(record.get("id") or ""))
        if carried:
            sheet_row[DECISION], sheet_row[REASON] = carried
        values.append(sheet_row)
    if len(values) == 1:
        values.append(["" for _ in HEADERS])

    api.clear(spreadsheetId=spreadsheet_id, range=f"{TAB_TITLE}!A1:K").execute()
    api.update(
        spreadsheetId=spreadsheet_id,
        range=f"{TAB_TITLE}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    read_back = read_tab_rows(api, spreadsheet_id)
    if not read_back or read_back[0] != HEADERS:
        raise RuntimeError("Confirmations tab read-back did not match the written headers")
    read_ids = {_cell(r, ID) for r in read_back[1:] if _cell(r, ID)}
    expected_ids = {str(r.get("id") or "") for r in records if r.get("id")}
    if read_ids != expected_ids:
        raise RuntimeError(
            f"Confirmations tab read-back IDs {sorted(read_ids)} "
            f"did not match ledger {sorted(expected_ids)}"
        )
    return {
        "tab": TAB_TITLE,
        "expired": expired,
        "decisions_applied": len(applied),
        "applied": applied,
        "rows": len(records),
        "read_back_rows": len(read_back) - 1,
        "notifications": _notify_pending(
            store, records, spreadsheet_id,
            notify=notify, chat_poster=chat_poster, gmail_sender=gmail_sender,
            chat_thread_poster=chat_thread_poster, zap_trigger=zap_trigger,
        ),
    }


def _notify_pending(
    store: Any,
    records: list[dict[str, Any]],
    spreadsheet_id: str,
    *,
    notify: bool,
    chat_poster: Any,
    gmail_sender: Any,
    chat_thread_poster: Any = None,
    zap_trigger: Any = None,
) -> list[dict[str, Any]]:
    """Fan out Chat + email for PENDING records not yet notified."""
    if not notify:
        return []
    from . import confirmation_notify

    results: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("status")) != "PENDING":
            continue
        confirmation_id = str(record.get("id") or "")
        if not confirmation_id:
            continue
        results.append(
            confirmation_notify.notify_requested(
                store,
                confirmation_id,
                chat_poster=chat_poster,
                gmail_sender=gmail_sender,
                chat_thread_poster=chat_thread_poster,
                zap_trigger=zap_trigger,
                given_sheet_id=spreadsheet_id,
            )
        )
    return results
