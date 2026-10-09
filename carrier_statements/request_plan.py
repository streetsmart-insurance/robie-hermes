"""Missing-statement draft policy. Pure planning; no send or scheduling authority."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr


def _address(value):
    if not isinstance(value, str) or any(x in value for x in "\r\n"):
        return None
    parsed = parseaddr(value)[1]
    if parsed != value or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
        return None
    return value.casefold()


def _stamp(value):
    if not isinstance(value, str): return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except ValueError:
        return None


def plan_missing_request(item: dict, *, now: datetime, max_evidence_age=timedelta(hours=24)) -> dict:
    """Prepare one initial request only after exact source/contact evidence review.

    Freshness policy is supplied explicitly by the operator; 24h is a draft
    test default, not a released business rule. No automatic chase timing is
    inferred from the older handoff. Input attestations are not authenticated
    by this pure function and cannot grant execution authority.
    """
    if now.tzinfo is None or max_evidence_age <= timedelta(0):
        raise ValueError("explicit_clock_and_freshness_required")
    now = now.astimezone(timezone.utc)
    result = {"status": "HELD", "holds": [], "send_enabled": False,
              "draft": None, "request_key": None}
    # Suppression is decisive and precedes draft generation.
    for key, reason in (("statement_received", "STATEMENT_ALREADY_RECEIVED"),
                        ("active_request", "REQUEST_ALREADY_ACTIVE"),
                        ("send_outcome_unknown", "PRIOR_SEND_OUTCOME_UNKNOWN"),
                        ("staff_work_in_progress", "STAFF_ALREADY_WORKING"),
                        ("newer_reply", "NEWER_REPLY_REVIEW_REQUIRED")):
        if item.get(key) is True:
            return {**result, "status": "SUPPRESSED", "holds": [reason]}
    if item.get("monthly_obligation") == "not_monthly":
        return {**result, "status": "NOT_APPLICABLE", "holds": ["NO_MONTHLY_OBLIGATION"]}
    if item.get("monthly_obligation") != "verified_monthly":
        result["holds"].append("MONTHLY_OBLIGATION_UNVERIFIED")
    for key in ("entity_id", "agency_id", "carrier_id", "account_reference"):
        if not isinstance(item.get(key), str) or not item[key].strip() or any(x in item[key] for x in "\r\n"):
            result["holds"].append("EXACT_IDENTITY_UNVERIFIED")
    period = item.get("printed_period")
    if not isinstance(period, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        result["holds"].append("REQUEST_PERIOD_UNVERIFIED")
    elif period >= now.strftime("%Y-%m"):
        result["holds"].append("PRIOR_PERIOD_REQUIRED")
    sender, contact = _address(item.get("sender")), _address(item.get("contact"))
    if sender is None or contact is None or sender == contact:
        result["holds"].append("SENDER_OR_RECIPIENT_INVALID")
    for key in ("contact_verified", "existing_store_checked", "portal_checked", "mail_search_complete"):
        if item.get(key) is not True:
            result["holds"].append(key.upper() + "_REQUIRED")
    checked = _stamp(item.get("evidence_checked_at"))
    if checked is None or checked > now or now - checked > max_evidence_age:
        result["holds"].append("SOURCE_EVIDENCE_STALE_OR_UNVERIFIED")
    # Presence of exact evidence references is required; references stay private.
    references = item.get("evidence_references")
    if not isinstance(references, list) or not references or not all(isinstance(x, str) and x.strip() for x in references):
        result["holds"].append("SOURCE_REFERENCES_REQUIRED")
    if item.get("thread_id") is not None:
        if not isinstance(item["thread_id"], str) or not item["thread_id"].strip() or item.get("thread_verified") is not True:
            result["holds"].append("THREAD_IDENTITY_UNVERIFIED")
    # Missing booleans cannot be interpreted as proof nothing has happened.
    for key in ("statement_received", "active_request", "send_outcome_unknown", "staff_work_in_progress", "newer_reply"):
        if item.get(key) is not False:
            result["holds"].append("REQUEST_PRECONDITIONS_UNVERIFIED")
    if result["holds"]:
        result["holds"] = sorted(set(result["holds"]))
        return result
    identity = [item[k] for k in ("entity_id", "agency_id", "carrier_id", "account_reference", "printed_period")]
    request_key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    # Contact/content changes do not reset the carrier-period duplicate key.
    return {**result, "status": "DRAFT_READY_REVIEW_ONLY", "request_key": request_key,
            "draft": {"sender": sender, "to": contact, "thread_id": item.get("thread_id"),
                      "subject": f"Monthly statement request — {period}",
                      "text_body": f"Please send the {period} monthly statement for agency account {item['account_reference']}. Thank you."},
            "evidence_references": list(references)}
