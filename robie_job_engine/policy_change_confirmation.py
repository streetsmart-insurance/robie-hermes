"""Read-only policy-change confirmation pilot.

A CSR assigns an existing EZLynx policy-change task to ROBIE. This job
resolves that exact case and the original assigner, compares the request,
the carrier-issued endorsement, and the EZLynx record, and drafts a note.

It does not file the note, upload a document, change a label, reassign the
task, send email, submit a portal, change the policy, confirm the change,
or close the task. It does not open a new Change Request form.

This is not the weekly 4359 overdue checker and not
``policy_change_verification``. Those paths stay as they are.

Carlo, October 1 2026: the producer confirms and closes. The original
assigner still receives the result. That role choice is resolved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .policy_change_ezlynx_read import apply_ezlynx_read


JOB_TYPE = "policy_change_confirmation"
WORKER_NAME = "policy-change-confirmation"
CHECKPOINT = "policy_change_confirmation_packet"
PROVISIONAL_CARRIER = "Progressive"
NOTE_SIGNATURE = "ROBIE was here"
WRITES_ENABLED = False
EZLYNX_FILED_SOURCE = "ezlynx_filed_carrier_document"
FILED_SOURCE_STATEMENT = (
    "This endorsement was already filed in EZLynx. The live carrier download "
    "route was not checked, and Progressive is not confirmed."
)

ROLE_DECISION = {
    "status": "resolved",
    "date": "2026-10-01",
    "decided_by": "Carlo",
    "decision": (
        "The producer is the authorized human who confirms and closes "
        "policy-change confirmations."
    ),
    "original_assigner": "receives the result",
    "writes": "disabled",
}

OUTCOMES = (
    "ready_for_human_review",
    "request_unclear",
    "waiting_for_carrier",
    "evidence_invalid",
    "carrier_correction_required",
    "ezlynx_correction_required",
    "coverage_review_required",
    "directory_incomplete",
    "destination_unverified",
    "stale_context",
    "retrieval_blocked",
    "writeback_unverified",
)

VERDICTS = (
    "exact_match",
    "normalized_match",
    "mismatch",
    "missing",
    "unknown",
    "not_applicable",
    "unrequested_change",
)

# Headline when more than one condition is true. Every condition still stays
# in the result. Nothing here turns a mixed packet into a pass.
_OUTCOME_PRIORITY = (
    "writeback_unverified",
    "stale_context",
    "retrieval_blocked",
    "directory_incomplete",
    "destination_unverified",
    "request_unclear",
    "evidence_invalid",
    "waiting_for_carrier",
    "coverage_review_required",
    "carrier_correction_required",
    "ezlynx_correction_required",
    "ready_for_human_review",
)

_HOLD_STATUS = {
    "ready_for_human_review": JobStatus.AWAITING_HUMAN_INPUT,
    "request_unclear": JobStatus.NEEDS_CLARIFICATION,
    "waiting_for_carrier": JobStatus.WAITING,
    "evidence_invalid": JobStatus.WAITING,
    "carrier_correction_required": JobStatus.AWAITING_HUMAN_INPUT,
    "ezlynx_correction_required": JobStatus.AWAITING_HUMAN_INPUT,
    "coverage_review_required": JobStatus.AWAITING_HUMAN_INPUT,
    "directory_incomplete": JobStatus.NEEDS_CLARIFICATION,
    "destination_unverified": JobStatus.NEEDS_CLARIFICATION,
    "stale_context": JobStatus.WAITING,
    "retrieval_blocked": JobStatus.WAITING,
    "writeback_unverified": JobStatus.WAITING,
}

_ROBIE_OWNERS = frozenset({"ssrobie", "robie", "robie ai"})

_REQUIRED_CASE_FIELDS = (
    "agency",
    "applicant_id",
    "policy_id",
    "policy_number",
    "carrier",
    "product",
    "term",
    "change_request_id",
    "task_id",
    "discussion_id",
    "assignment_event_id",
    "assignment_timestamp",
    "request_date",
    "requested_effective_date",
    "due_date",
    "submission_evidence",
)

_CORE_FIELDS = (
    ("policy_number", "identity", "id", "policy number"),
    ("term", "identity", "id", "policy term"),
    ("named_insured", "identity", "legal_name", "named insured"),
    ("carrier", "identity", "text", "carrier"),
    ("product", "identity", "text", "product"),
    ("change_action", "identity", "text", "change"),
    ("effective_date", "identity", "date", "effective date"),
    ("vin", "scheduled", "vin", "vehicle identification number"),
    ("garaging_address", "address", "address", "garaging address"),
    ("limit", "coverage", "money", "limit"),
    ("premium_amount", "premium", "money", "premium"),
    ("premium_basis", "premium", "basis", "premium basis"),
)

_NOT_ISSUED = frozenset({"quote", "acknowledgement", "acknowledgment", "carrier_processed"})

_EXCLUSIONS = {
    "quote_only": "waiting_for_carrier",
    "unsubmitted": "waiting_for_carrier",
    "expired_rewrite": "request_unclear",
    "cancellation": "request_unclear",
    "disputed": "request_unclear",
    "backdated": "request_unclear",
    "ambiguous_authority": "request_unclear",
    "missing_signature": "request_unclear",
    "agency_bill": "request_unclear",
    "legal_entity_change": "request_unclear",
    "licensed_coverage_decision": "coverage_review_required",
    "multi_policy_incomplete": "request_unclear",
    "carrier_never_issues_endorsement": "waiting_for_carrier",
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y",
    "%b %d, %Y",
)

_BASIS_ALIASES = {
    "transaction": "transaction",
    "transaction premium": "transaction",
    "full-term": "full-term",
    "full term": "full-term",
    "full-term premium": "full-term",
    "installment": "installment",
    "installments": "installment",
    "unspecified": "unspecified",
}

_PLAIN_RESULT = {
    "ready_for_human_review": "Ready for the producer to confirm and close.",
    "request_unclear": "The request is not clear enough to check.",
    "waiting_for_carrier": "Waiting on the carrier's issued endorsement.",
    "evidence_invalid": "The documents are not complete or readable enough to check.",
    "carrier_correction_required": "The carrier's endorsement does not match the request.",
    "ezlynx_correction_required": "EZLynx does not match what the carrier issued.",
    "coverage_review_required": "A licensed producer needs to review the coverage.",
    "directory_incomplete": "The carrier download route is not on file.",
    "destination_unverified": "The case or the person who assigned it is not exact.",
    "stale_context": "The case changed while this check was running. It needs a fresh read.",
    "retrieval_blocked": "The documents could not be read.",
    "writeback_unverified": "A save could not be confirmed, so this is not finished.",
}


def progressive_access_proof(proof: Mapping[str, Any] | None) -> dict[str, Any]:
    """Confirm Progressive only with Test access, the live route, and a real endorsement.

    A For Agents Only memo is not an endorsement. A missing proof stays provisional.
    This function does not contact EZLynx or the carrier.
    """
    supplied = dict(proof or {})
    missing: list[str] = []
    expected = {
        "worker_identity": "SSRobie",
        "environment": "TEST",
        "host": "hermes-test-01",
        "directory_route_verified": True,
        "genuine_issued_endorsement": True,
    }
    for key, value in expected.items():
        if supplied.get(key) != value:
            missing.append(key)
    if supplied.get("memo_only") is True or supplied.get("source") == "fao_memo":
        missing.append("memo_retrieval_is_not_an_endorsement")
    if not str(supplied.get("endorsement_document_id") or "").strip():
        missing.append("endorsement_document_id")
    # A document already stored in EZLynx is not a walk of the Directory route.
    if supplied.get("source") == EZLYNX_FILED_SOURCE and supplied.get("directory_route_walked") is not True:
        if "directory_route_verified" not in missing:
            missing.append("directory_route_verified")
    confirmed = not missing
    return {
        "carrier": PROVISIONAL_CARRIER,
        "status": "confirmed" if confirmed else "provisional",
        "confirmed": confirmed,
        "missing": missing,
        "reason": (
            "Test access, the Directory route, and a genuine issued endorsement were supplied."
            if confirmed
            else (
                "Progressive stays provisional. Test access under SSRobie, a verified "
                "Directory Document Download route, and a genuine issued endorsement "
                "are required. A memo retrieval does not prove an endorsement."
            )
        ),
    }


class DisabledWrites:
    """Counts write attempts and performs none."""

    def __init__(self) -> None:
        self.refused: list[str] = []
        self.performed = 0

    def refuse(self, name: str) -> bool:
        self.refused.append(str(name))
        return False

    @property
    def external_writes(self) -> int:
        return self.performed


class ConfirmationLedger:
    """One stored output per case. A second call replays it or stops."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        item = self.items.get(key)
        return None if item is None else dict(item)

    def put_once(self, key: str, record: Mapping[str, Any]) -> bool:
        if key in self.items:
            return False
        self.items[key] = dict(record)
        return True

    def __len__(self) -> int:
        return len(self.items)


def confirmation_idempotency_key(packet: Mapping[str, Any]) -> str:
    return "policy-change-confirmation:" + case_key(packet)


def case_key(packet: Mapping[str, Any]) -> str:
    case = dict(packet.get("case") or {})
    parts = (
        str(packet.get("task_id") or case.get("task_id") or "").strip(),
        str(packet.get("assignment_event_id") or case.get("assignment_event_id") or "").strip(),
        str(case.get("policy_id") or "").strip(),
    )
    return "|".join(parts)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def source_hash(packet: Mapping[str, Any]) -> str:
    material = {
        "case": packet.get("case"),
        "task": packet.get("task"),
        "assignment_events": packet.get("assignment_events"),
        "discussions": packet.get("discussions"),
        "request": packet.get("request"),
        "carrier_document": packet.get("carrier_document"),
        "ezlynx_record": packet.get("ezlynx_record"),
        "linked_applicant": packet.get("linked_applicant"),
        "filing": packet.get("filing"),
        "readback": packet.get("readback"),
        "reread": packet.get("reread"),
        "directory_entry": packet.get("directory_entry"),
        "exclusions": packet.get("exclusions"),
        "write_claim": packet.get("write_claim"),
        "open_change_request_form": packet.get("open_change_request_form"),
        "ezlynx_snapshot": packet.get("ezlynx_snapshot"),
        "carrier_proof": packet.get("carrier_proof"),
    }
    return hashlib.sha256(_json(material).encode()).hexdigest()


def _is_robie(value: Any) -> bool:
    return str(value or "").strip().casefold() in _ROBIE_OWNERS


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _collapse(value: Any) -> str:
    return " ".join(str(value).split())


def _normalize(kind: str, raw: Any) -> tuple[Any, str]:
    if raw is None:
        return None, "none"
    if kind == "date":
        text = _collapse(raw)
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date().isoformat(), "date"
            except ValueError:
                continue
        return text, "unparsed"
    if kind == "money":
        text = str(raw).strip()
        negative = text.startswith("(") and text.endswith(")")
        cleaned = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
        if negative and cleaned and not cleaned.startswith("-"):
            cleaned = "-" + cleaned
        if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned or ""):
            return str(raw), "unparsed"
        whole, _, frac = cleaned.partition(".")
        frac = (frac + "00")[:2]
        normalized = f"{whole}.{frac}"
        rule = "money" if normalized != str(raw).strip() else "none"
        return normalized, rule
    if kind == "vin":
        text = _collapse(raw).upper()
        return text, "vin" if text != str(raw) else "none"
    if kind == "legal_name":
        text = _collapse(raw).casefold()
        return text, "legal_name" if text != str(raw) else "none"
    if kind == "basis":
        key = _collapse(raw).casefold()
        if key not in _BASIS_ALIASES:
            return key, "unparsed"
        mapped = _BASIS_ALIASES[key]
        return mapped, "basis" if mapped != str(raw).strip() else "none"
    if kind == "address":
        if not isinstance(raw, dict):
            return str(raw), "unparsed"
        parts = {}
        for part in ("street", "unit", "city", "state", "postal"):
            item = raw.get(part)
            parts[part] = "" if _blank(item) else _collapse(item).casefold()
        displayed = {part: "" if _blank(raw.get(part)) else str(raw.get(part)) for part in parts}
        rule = "address" if parts != {k: v.casefold() if isinstance(v, str) else v for k, v in displayed.items()} else "none"
        return parts, rule
    if kind == "id":
        text = _collapse(raw)
        return text, "whitespace" if text != str(raw) else "none"
    text = _collapse(raw).casefold()
    return text, "text" if text != str(raw) else "none"


def _cell(source: Mapping[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    fields = source.get("fields")
    if not isinstance(fields, Mapping) or key not in fields:
        return None
    value = fields[key]
    if isinstance(value, Mapping) and "raw" in value:
        return {
            "raw": value.get("raw"),
            "source_id": str(value.get("source_id") or source.get("source_id") or ""),
            "reference": str(value.get("reference") or ""),
            "absent": bool(value.get("absent")),
            "absence_reason": str(value.get("absence_reason") or ""),
            "semantic_uncertain": bool(value.get("semantic_uncertain")),
        }
    return {
        "raw": value,
        "source_id": str(source.get("source_id") or ""),
        "reference": str(source.get("reference") or ""),
        "absent": False,
        "absence_reason": "",
        "semantic_uncertain": False,
    }


def _present_raw(cell: Mapping[str, Any] | None) -> Any:
    if cell is None:
        return None
    return cell.get("raw")


def _compare_pair(kind: str, left: Any, right: Any) -> tuple[str, str, Any, Any]:
    left_norm, left_rule = _normalize(kind, left)
    right_norm, right_rule = _normalize(kind, right)
    if left_rule == "unparsed" or right_rule == "unparsed":
        if str(left) == str(right):
            return "exact_match", "none", left_norm, right_norm
        return "unknown", "unparsed", left_norm, right_norm
    if left_norm == right_norm:
        if left == right:
            return "exact_match", "none", left_norm, right_norm
        rule = left_rule if left_rule != "none" else right_rule
        return "normalized_match", rule, left_norm, right_norm
    return "mismatch", "none", left_norm, right_norm


def _row_verdict(kind: str, request: dict[str, Any] | None, carrier: dict[str, Any] | None, ezlynx: dict[str, Any] | None) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    slots = {"request": request, "carrier": carrier, "ezlynx": ezlynx}
    packed: dict[str, Any] = {}
    for name, cell in slots.items():
        if cell is None:
            packed[name] = {"raw": None, "normalized": None, "source_id": "", "reference": "", "state": "missing"}
            continue
        if cell.get("semantic_uncertain"):
            packed[name] = {
                "raw": cell.get("raw"),
                "normalized": None,
                "source_id": cell.get("source_id"),
                "reference": cell.get("reference"),
                "state": "unknown",
            }
            continue
        if _blank(cell.get("raw")) and not cell.get("absent"):
            packed[name] = {
                "raw": cell.get("raw"),
                "normalized": None,
                "source_id": cell.get("source_id"),
                "reference": cell.get("reference"),
                "state": "unknown",
            }
            continue
        if cell.get("absent"):
            if not cell.get("absence_reason"):
                packed[name] = {
                    "raw": None,
                    "normalized": None,
                    "source_id": cell.get("source_id"),
                    "reference": cell.get("reference"),
                    "state": "unknown",
                }
            else:
                packed[name] = {
                    "raw": None,
                    "normalized": None,
                    "source_id": cell.get("source_id"),
                    "reference": cell.get("reference"),
                    "state": "not_applicable",
                    "reason": cell.get("absence_reason"),
                }
            continue
        normalized, rule = _normalize(kind, cell.get("raw"))
        packed[name] = {
            "raw": cell.get("raw"),
            "normalized": normalized,
            "source_id": cell.get("source_id"),
            "reference": cell.get("reference"),
            "state": "present",
            "rule": rule,
        }

    if any(item.get("state") == "unknown" for item in packed.values()):
        return "unknown", "unparsed" if kind == "address" else "none", packed["request"], packed["carrier"], packed["ezlynx"]
    present = [name for name in ("request", "carrier", "ezlynx") if packed[name].get("state") == "present"]
    if len(present) >= 2 and any(item.get("state") == "missing" for item in packed.values()):
        mismatched = False
        unknown_pair = False
        for left_name, right_name in zip(present, present[1:]):
            verdict, _rule, _left, _right = _compare_pair(
                kind, packed[left_name]["raw"], packed[right_name]["raw"]
            )
            if verdict == "mismatch":
                mismatched = True
            elif verdict == "unknown":
                unknown_pair = True
        if mismatched:
            return "mismatch", "none", packed["request"], packed["carrier"], packed["ezlynx"]
        if unknown_pair:
            return "unknown", "unparsed", packed["request"], packed["carrier"], packed["ezlynx"]
        return "missing", "none", packed["request"], packed["carrier"], packed["ezlynx"]
    if any(item.get("state") == "missing" for item in packed.values()):
        return "missing", "none", packed["request"], packed["carrier"], packed["ezlynx"]
    if all(item.get("state") == "not_applicable" for item in packed.values()):
        reason = packed["request"].get("reason") or "not applicable"
        return "not_applicable", reason, packed["request"], packed["carrier"], packed["ezlynx"]

    request_raw = packed["request"]["raw"]
    if kind in {"money", "basis"} and _normalize(kind, request_raw)[0] == "unspecified":
        verdict, rule, _, _ = _compare_pair(kind, packed["carrier"]["raw"], packed["ezlynx"]["raw"])
        explanation_rule = rule
        return verdict, explanation_rule, packed["request"], packed["carrier"], packed["ezlynx"]

    first, rule, _, _ = _compare_pair(kind, packed["request"]["raw"], packed["carrier"]["raw"])
    second, rule_2, _, _ = _compare_pair(kind, packed["carrier"]["raw"], packed["ezlynx"]["raw"])
    if first == "unknown" or second == "unknown":
        return "unknown", "unparsed", packed["request"], packed["carrier"], packed["ezlynx"]
    if first == "mismatch" or second == "mismatch":
        return "mismatch", "none", packed["request"], packed["carrier"], packed["ezlynx"]
    if first == "normalized_match" or second == "normalized_match":
        rule_name = rule if rule not in {"none", ""} else rule_2
        return "normalized_match", rule_name, packed["request"], packed["carrier"], packed["ezlynx"]
    return "exact_match", "none", packed["request"], packed["carrier"], packed["ezlynx"]


def _explain(label: str, verdict: str, request: Mapping[str, Any], carrier: Mapping[str, Any], ezlynx: Mapping[str, Any], rule: str) -> str:
    if verdict == "exact_match":
        return f"The {label} matches in the request, the endorsement, and EZLynx."
    if verdict == "normalized_match":
        return f"The {label} matches after ordinary formatting is ignored."
    if verdict == "mismatch":
        def _shown(side: Mapping[str, Any]) -> Any:
            if side.get("state") == "missing" or side.get("raw") in (None, ""):
                return "not read"
            return side.get("raw")

        return (
            f"The {label} differs. Request: {_shown(request)}. "
            f"Endorsement: {_shown(carrier)}. EZLynx: {_shown(ezlynx)}."
        )
    if verdict == "missing":
        return f"The {label} is missing from one of the three sources."
    if verdict == "unknown":
        return f"The {label} cannot be read, so it was not filled in."
    if verdict == "not_applicable":
        return str(rule or "This item does not apply.")
    if verdict == "unrequested_change":
        shown = carrier.get("raw") if carrier.get("raw") not in (None, "") else ezlynx.get("raw")
        return f"The endorsement or EZLynx shows {label}: {shown}. The request did not ask for that."
    return f"The {label} was checked."


def build_comparison(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    request = packet.get("request") if isinstance(packet.get("request"), Mapping) else {}
    carrier = packet.get("carrier_document") if isinstance(packet.get("carrier_document"), Mapping) else {}
    ezlynx = packet.get("ezlynx_record") if isinstance(packet.get("ezlynx_record"), Mapping) else {}
    rows: list[dict[str, Any]] = []
    for key, group, kind, label in _CORE_FIELDS:
        verdict, rule, req, car, ez = _row_verdict(
            kind, _cell(request, key), _cell(carrier, key), _cell(ezlynx, key)
        )
        row = {
            "field_group": group,
            "field": key,
            "label": label,
            "request": req,
            "carrier": car,
            "ezlynx": ez,
            "normalization": rule,
            "verdict": verdict,
            "explanation": _explain(label, verdict, req, car, ez, rule),
        }
        if key == "effective_date":
            requested = req.get("normalized")
            issued = car.get("normalized")
            if (
                req.get("state") == "present"
                and car.get("state") == "present"
                and requested
                and issued
                and requested != issued
            ):
                row["date_mismatch"] = True
                row["requested_effective_date"] = requested
                row["issued_effective_date"] = issued
        rows.append(row)
    linked = packet.get("linked_applicant") if isinstance(packet.get("linked_applicant"), Mapping) else None
    if linked and not _blank(linked.get("name")):
        insured = _cell(request, "named_insured")
        policy_name = "" if insured is None else str(insured.get("raw") or "")
        rows.append(
            {
                "field_group": "context",
                "field": "linked_applicant",
                "label": "linked applicant",
                "request": {
                    "raw": linked.get("name"),
                    "normalized": None,
                    "source_id": str(linked.get("source_id") or ""),
                    "reference": str(linked.get("reference") or "linked applicants"),
                    "state": "present",
                },
                "carrier": {
                    "raw": policy_name,
                    "normalized": None,
                    "source_id": str((insured or {}).get("source_id") or ""),
                    "reference": "policy named insured",
                    "state": "present",
                },
                "ezlynx": {
                    "raw": policy_name,
                    "normalized": None,
                    "source_id": "",
                    "reference": "policy named insured",
                    "state": "present",
                },
                "normalization": "not used as the policy name",
                "verdict": "not_applicable",
                "explanation": (
                    "The linked applicant name was not used. The name on the policy "
                    "is the one that was checked."
                ),
            }
        )
    for side_name, source in (("carrier", carrier), ("ezlynx", ezlynx)):
        extra = source.get("unrequested") if isinstance(source, Mapping) else None
        if not isinstance(extra, list):
            continue
        for item in extra:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("raw")
            label = str(item.get("label") or "change")
            rows.append(
                {
                    "field_group": str(item.get("group") or "coverage"),
                    "field": "unrequested_change",
                    "label": label,
                    "request": {
                        "raw": "not requested",
                        "normalized": "not requested",
                        "source_id": str(request.get("source_id") or ""),
                        "reference": "request",
                        "state": "present",
                    },
                    "carrier": {
                        "raw": raw if side_name == "carrier" else None,
                        "normalized": raw if side_name == "carrier" else None,
                        "source_id": str(item.get("source_id") or source.get("source_id") or ""),
                        "reference": str(item.get("reference") or ""),
                        "state": "present" if side_name == "carrier" else "missing",
                    },
                    "ezlynx": {
                        "raw": raw if side_name == "ezlynx" else None,
                        "normalized": raw if side_name == "ezlynx" else None,
                        "source_id": str(item.get("source_id") or source.get("source_id") or ""),
                        "reference": str(item.get("reference") or ""),
                        "state": "present" if side_name == "ezlynx" else "missing",
                    },
                    "normalization": "none",
                    "verdict": "unrequested_change",
                    "explanation": _explain(label, "unrequested_change", {}, {"raw": raw}, {"raw": raw}, "none"),
                    "coverage": bool(item.get("coverage")),
                    "side": side_name,
                }
            )
    return rows


def _assignment_event(packet: Mapping[str, Any]) -> dict[str, Any] | None:
    wanted = str(packet.get("assignment_event_id") or (packet.get("case") or {}).get("assignment_event_id") or "").strip()
    events = packet.get("assignment_events")
    if not wanted or not isinstance(events, list):
        return None
    matches = [item for item in events if isinstance(item, Mapping) and str(item.get("id") or "") == wanted]
    if len(matches) != 1:
        return None
    return dict(matches[0])


def _case_problems(packet: Mapping[str, Any]) -> list[str]:
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    problems = []
    for field in _REQUIRED_CASE_FIELDS:
        if _blank(case.get(field)):
            problems.append(field)
    return problems


def _resolve_assigner(packet: Mapping[str, Any], event: Mapping[str, Any]) -> tuple[str, str]:
    """Return (assigner id, assigner name) from the assignment event only."""
    previous = str(event.get("previous_owner_id") or "").strip()
    name = str(event.get("previous_owner_name") or "").strip()
    if not previous or _is_robie(previous):
        return "", ""
    claimed = str((packet.get("case") or {}).get("original_assigner_id") or "").strip()
    if claimed and claimed != previous:
        return "", ""
    return previous, name


def _discussion_problem(packet: Mapping[str, Any]) -> str | None:
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    discussions = packet.get("discussions")
    if not isinstance(discussions, list):
        return "The discussion was not read."
    if len(discussions) != 1:
        return "More than one discussion is on this case, so none was chosen."
    discussion = discussions[0] if isinstance(discussions[0], Mapping) else {}
    if str(discussion.get("id") or "") != str(case.get("discussion_id") or ""):
        return "The discussion does not match this case."
    return None


def _reread_problem(packet: Mapping[str, Any], event: Mapping[str, Any]) -> str | None:
    reread = packet.get("reread")
    if not isinstance(reread, Mapping):
        return "The task owner was not read again before the result."
    if not _is_robie(reread.get("current_owner_id")):
        return "The task is no longer assigned to ROBIE."
    if str(reread.get("assignment_event_id") or "") != str(event.get("id") or ""):
        return "The assignment changed while this check was running."
    if str(reread.get("source_version") or "") != str(packet.get("source_version") or ""):
        return "The source version changed while this check was running."
    return None


def _document_problem(packet: Mapping[str, Any]) -> str | None:
    document = packet.get("carrier_document")
    if not isinstance(document, Mapping) or not document:
        entry = packet.get("directory_entry")
        if not isinstance(entry, Mapping) or _blank(entry.get("document_download_route")):
            return "directory_incomplete"
        return "retrieval_blocked"
    kind = str(document.get("kind") or "").strip().casefold()
    if kind in _NOT_ISSUED or document.get("issued") is not True:
        return "waiting_for_carrier"
    if document.get("pages_missing") or str(document.get("ocr_quality") or "").strip().casefold() in {"weak", "unreadable"}:
        return "evidence_invalid"
    return None


def _exclusion_outcome(packet: Mapping[str, Any]) -> str | None:
    flags = packet.get("exclusions")
    if not isinstance(flags, Mapping):
        return None
    found = []
    for name, outcome in _EXCLUSIONS.items():
        if flags.get(name) is True:
            found.append(outcome)
    if not found:
        return None
    return _headline(found)


def _writeback_problem(packet: Mapping[str, Any]) -> str | None:
    claim = packet.get("write_claim")
    if claim not in (None, "", False):
        return "A write was claimed and this pilot cannot confirm it."
    filing = packet.get("filing") if isinstance(packet.get("filing"), Mapping) else {}
    readback = packet.get("readback") if isinstance(packet.get("readback"), Mapping) else {}
    document_id = str(filing.get("document_id") or "").strip()
    if filing.get("verified") is True or document_id:
        if readback.get("uncertain") is True or readback.get("ok") is not True:
            return "The filed document could not be read back."
        if str(readback.get("document_id") or "") != document_id:
            return "The read-back document is not the filed document."
    return None


def _headline(outcomes: list[str]) -> str:
    present = set(outcomes)
    for outcome in _OUTCOME_PRIORITY:
        if outcome in present:
            return outcome
    return "request_unclear"


def _exception_owner(outcome: str) -> str:
    if outcome in {
        "ready_for_human_review",
        "carrier_correction_required",
        "ezlynx_correction_required",
        "coverage_review_required",
    }:
        return "producer"
    if outcome == "waiting_for_carrier":
        return "carrier"
    return "original_assigner"


def _next_action(outcome: str, assigner_name: str) -> str:
    who = assigner_name or "the person who assigned this task"
    if outcome == "ready_for_human_review":
        return f"The producer confirms and closes this. {who} receives the result. The task stays open."
    if outcome == "carrier_correction_required":
        return f"The producer reviews the endorsement difference. {who} receives the result."
    if outcome == "ezlynx_correction_required":
        return "Correct the agency record, then read it again. The producer confirms after that reread."
    if outcome == "coverage_review_required":
        return "The producer reviews the coverage before anything is confirmed."
    if outcome == "waiting_for_carrier":
        return "Obtain the issued endorsement. An acknowledgement is not enough."
    if outcome == "evidence_invalid":
        return "Obtain a complete, readable endorsement. Missing values were not filled in."
    if outcome == "directory_incomplete":
        return "Confirm the carrier download route before this check continues."
    if outcome == "destination_unverified":
        return "Resolve the exact case and the person who assigned it. Do not guess."
    if outcome == "stale_context":
        return "Read the case again. The earlier result was not reused."
    if outcome == "retrieval_blocked":
        return "Retrieve the issued endorsement on Test as SSRobie. Use the existing Gemini rescue if a screen is stuck."
    if outcome == "writeback_unverified":
        return "Read the actual saved item before any retry. Do not treat this as finished."
    return "Obtain a clearer request or the missing support."


def draft_note(packet: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    request = packet.get("request") if isinstance(packet.get("request"), Mapping) else {}
    carrier = packet.get("carrier_document") if isinstance(packet.get("carrier_document"), Mapping) else {}
    ezlynx = packet.get("ezlynx_record") if isinstance(packet.get("ezlynx_record"), Mapping) else {}
    filing = packet.get("filing") if isinstance(packet.get("filing"), Mapping) else {}
    insured = _present_raw(_cell(request, "named_insured")) or "The named insured"
    number = case.get("policy_number") or _present_raw(_cell(request, "policy_number")) or "an unnamed policy"
    term = case.get("term") or ""
    effective = case.get("requested_effective_date") or ""
    requested = str(request.get("summary") or "").strip() or "The requested change was not spelled out."
    issued = str(carrier.get("summary") or "").strip()
    if not issued:
        issued = "An issued endorsement was not available for this check." if not result.get("comparison_complete") else "The endorsement was read with the request."
    recorded = str(ezlynx.get("summary") or "").strip() or "The EZLynx record was not fully described."
    rows = list(result.get("comparison") or [])
    if result.get("comparison_complete"):
        match_lines = [row["explanation"] for row in rows if row.get("verdict") in {"exact_match", "normalized_match"}]
        linked = [row["explanation"] for row in rows if row.get("field") == "linked_applicant"]
        matches = " ".join(match_lines + linked) if (match_lines or linked) else "The compared items match."
        problems = [row for row in rows if row.get("verdict") not in {"exact_match", "normalized_match", "not_applicable"}]
        if problems:
            exceptions = " ".join(row["explanation"] for row in problems)
        elif result.get("outcome") != "ready_for_human_review" and result.get("reason"):
            exceptions = str(result.get("reason"))
        else:
            exceptions = "No exceptions."
    else:
        matches = "The comparison is not finished."
        exceptions = "The check stopped before every item was compared."
    if filing:
        documents = (
            f"Saved name: {filing.get('name') or 'not recorded'}. "
            f"Folder: {filing.get('folder') or 'not recorded'}. "
            f"Label: {filing.get('label') or 'not recorded'}. "
            f"Policy: {filing.get('policy_number') or number}."
        )
    else:
        documents = "The endorsement file name, folder, and label were not recorded."
    source = result.get("carrier_source")
    if isinstance(source, Mapping) and source.get("statement"):
        documents = f"{documents} {source.get('statement')}"
    follow_up = packet.get("follow_up_date") or "not set"
    sections = [
        ("Policy and effective date", f"{insured}, policy {number}, term {term}, effective {effective}."),
        ("Requested", requested),
        ("Carrier issued", issued),
        ("EZLynx recorded", recorded),
        ("Matches", matches),
        ("Exceptions", exceptions),
        ("Documents", documents),
        ("Result", str(result.get("result_sentence") or "")),
        ("Next action and follow-up", f"{result.get('next_action') or ''} Follow-up: {follow_up}."),
    ]
    lines: list[str] = []
    for title, body in sections:
        lines.append(title)
        lines.append(str(body).strip())
        lines.append("")
    lines.append(NOTE_SIGNATURE)
    return "\n".join(lines).rstrip() + "\n"


def ezlynx_filed_carrier_source(packet: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept an endorsement already filed in EZLynx when the Directory route cannot run.

    The label is not proof that the live Directory route works, and it does
    not confirm Progressive.
    """
    proof = packet.get("carrier_proof") if isinstance(packet.get("carrier_proof"), Mapping) else {}
    document = packet.get("carrier_document") if isinstance(packet.get("carrier_document"), Mapping) else {}
    filing = packet.get("filing") if isinstance(packet.get("filing"), Mapping) else {}
    source = str(proof.get("source") or document.get("source") or "").strip()
    if source != EZLYNX_FILED_SOURCE:
        return None
    if proof.get("memo_only") is True or proof.get("genuine_issued_endorsement") is not True:
        return None
    if document.get("issued") is not True:
        return None
    kind = str(document.get("kind") or "").strip().casefold()
    if kind in _NOT_ISSUED:
        return None
    document_id = str(
        proof.get("endorsement_document_id")
        or filing.get("document_id")
        or document.get("source_id")
        or ""
    ).strip()
    if not document_id:
        return None
    return {
        "label": EZLYNX_FILED_SOURCE,
        "document_id": document_id,
        "directory_route_verified": False,
        "proves_directory_route": False,
        "progressive_confirmed": False,
        "statement": FILED_SOURCE_STATEMENT,
    }


def read_gaps(packet: Mapping[str, Any]) -> list[str]:
    """Fields this packet still does not have. Blank stays blank."""
    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    task = packet.get("task") if isinstance(packet.get("task"), Mapping) else {}
    record = packet.get("ezlynx_record") if isinstance(packet.get("ezlynx_record"), Mapping) else {}
    gaps = []
    if _blank(case.get("task_id")) and _blank(packet.get("task_id")) and _blank(task.get("id")):
        gaps.append("task_id")
    if _blank(case.get("due_date")):
        gaps.append("due_date")
    if _blank(task.get("current_owner_id")) and _blank(task.get("assignee_id")) and _blank(task.get("assignee_name")):
        gaps.append("assignee")
    if _blank(case.get("submission_evidence")):
        gaps.append("submission_evidence")
    if not record.get("vehicles"):
        gaps.append("ezlynx_vehicle_list")
    effective = _cell(record, "effective_date")
    if effective is None or _blank(effective.get("raw")):
        gaps.append("ezlynx_change_effective_date")
    return gaps


def _destination_failure(packet: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return (reason, assigner id, assigner name). Reason is empty when the case is exact."""
    problems = _case_problems(packet)
    event = _assignment_event(packet)
    task = packet.get("task") if isinstance(packet.get("task"), Mapping) else {}
    owner_known = event is not None and _is_robie(task.get("current_owner_id")) and _is_robie(event.get("current_owner_id"))
    if problems or not owner_known:
        return "The task assignment does not identify one exact case assigned to ROBIE.", "", ""
    assert event is not None
    assigner_id, assigner_name = _resolve_assigner(packet, event)
    discussion_problem = _discussion_problem(packet)
    if not assigner_id or discussion_problem:
        return discussion_problem or "The original assigner is not on the assignment event.", "", ""
    return "", assigner_id, assigner_name


def _finish(
    packet: Mapping[str, Any],
    *,
    outcomes: list[str],
    comparison: list[dict[str, Any]] | None,
    comparison_complete: bool,
    assigner_id: str,
    assigner_name: str,
    reason: str,
    writes: DisabledWrites,
    output_id: str | None,
    replayed: bool,
    blocked_target: str = "",
) -> dict[str, Any]:
    unique: list[str] = []
    for outcome in outcomes:
        if outcome not in unique:
            unique.append(outcome)
    headline = _headline(unique)
    review_ready = headline == "ready_for_human_review" and comparison_complete and writes.external_writes == 0
    if not review_ready and headline == "ready_for_human_review":
        headline = "request_unclear"
        if headline not in unique:
            unique.append(headline)
    result_sentence = _PLAIN_RESULT[headline]
    exceptions = []
    for row in comparison or []:
        if row.get("verdict") in {"exact_match", "normalized_match", "not_applicable"}:
            continue
        outcome = "carrier_correction_required"
        if row.get("verdict") == "unrequested_change" and row.get("coverage"):
            outcome = "coverage_review_required"
        elif row.get("verdict") == "unrequested_change" and row.get("side") == "ezlynx":
            outcome = "ezlynx_correction_required"
        elif row.get("verdict") == "mismatch":
            request_cell = row.get("request") or {}
            carrier_cell = row.get("carrier") or {}
            ez_cell = row.get("ezlynx") or {}
            request_raw = request_cell.get("raw")
            carrier_raw = carrier_cell.get("raw")
            ez_raw = ez_cell.get("raw")
            carrier_present = carrier_cell.get("state") == "present"
            ez_present = ez_cell.get("state") == "present"
            if carrier_present and ez_present and request_raw == carrier_raw and carrier_raw != ez_raw:
                outcome = "ezlynx_correction_required"
            elif not carrier_present and ez_present and request_raw != ez_raw:
                outcome = "ezlynx_correction_required"
        exceptions.append(
            {
                "summary": row.get("explanation"),
                "field": row.get("label"),
                "verdict": row.get("verdict"),
                "request": (row.get("request") or {}).get("raw"),
                "carrier": (row.get("carrier") or {}).get("raw"),
                "ezlynx": (row.get("ezlynx") or {}).get("raw"),
                "owner": _exception_owner(outcome),
                "next_action": _next_action(outcome, assigner_name),
                "evidence": {
                    "request": row.get("request"),
                    "carrier": row.get("carrier"),
                    "ezlynx": row.get("ezlynx"),
                },
            }
        )
    if reason and not any(item.get("summary") == reason for item in exceptions):
        exceptions.insert(
            0,
            {
                "summary": reason,
                "field": "",
                "verdict": "",
                "owner": _exception_owner(headline),
                "next_action": _next_action(headline, assigner_name),
                "evidence": {"target": blocked_target} if blocked_target else {},
            },
        )
    elif headline != "ready_for_human_review" and not exceptions:
        exceptions.append(
            {
                "summary": reason or result_sentence,
                "field": "",
                "verdict": "",
                "owner": _exception_owner(headline),
                "next_action": _next_action(headline, assigner_name),
                "evidence": {"target": blocked_target} if blocked_target else {},
            }
        )
    proof_input = packet.get("carrier_proof") if isinstance(packet.get("carrier_proof"), Mapping) else None
    carrier_pilot = progressive_access_proof(proof_input)
    carrier_source = ezlynx_filed_carrier_source(packet)
    if carrier_source is not None:
        carrier_pilot = dict(carrier_pilot)
        carrier_pilot["confirmed"] = False
        carrier_pilot["status"] = "provisional"
        carrier_pilot["carrier_source"] = carrier_source["label"]
    flags = []
    for row in comparison or []:
        if row.get("date_mismatch"):
            flags.append(
                {
                    "code": "effective_date_mismatch",
                    "requested": row.get("requested_effective_date"),
                    "issued": row.get("issued_effective_date"),
                    "ezlynx": (row.get("ezlynx") or {}).get("normalized"),
                    "field": "effective_date",
                }
            )
    draft = {
        "outcome": headline,
        "outcomes": unique,
        "exceptions": exceptions,
        "comparison": comparison or [],
        "comparison_complete": comparison_complete,
        "external_writes": writes.external_writes,
        "writes_enabled": WRITES_ENABLED,
        "refused_writes": list(writes.refused),
        "task_open": True,
        "change_request_open": True,
        "completed": False,
        "review_ready": review_ready,
        "replayed": replayed,
        "output_id": output_id,
        "confirmation_owner": "producer" if headline in {
            "ready_for_human_review",
            "carrier_correction_required",
            "ezlynx_correction_required",
            "coverage_review_required",
        } else None,
        "result_recipient": (
            {"id": assigner_id, "name": assigner_name, "role": "original_assigner"}
            if assigner_id
            else None
        ),
        "role_decision": dict(ROLE_DECISION),
        "carrier_pilot": carrier_pilot,
        "carrier_source": carrier_source,
        "flags": flags,
        "unread": read_gaps(packet),
        "live_carrier_retrieval": False,
        "live_test": "UNVERIFIED",
        "result_sentence": result_sentence,
        "next_action": _next_action(headline, assigner_name),
        "reason": reason,
        "blocked_target": blocked_target,
        "case_key": case_key(packet),
        "source_hash": source_hash(packet),
    }
    draft["note"] = draft_note(packet, draft)
    draft["evidence_hash"] = hashlib.sha256(draft["note"].encode()).hexdigest()
    return draft


def _evaluate(packet: Mapping[str, Any], writes: DisabledWrites) -> dict[str, Any]:
    if packet.get("write_claim") not in (None, "", False):
        writes.refuse("write_claim")
    if packet.get("open_change_request_form") is True:
        writes.refuse("open_change_request_form")
        return _finish(
            packet,
            outcomes=["retrieval_blocked"],
            comparison=[],
            comparison_complete=False,
            assigner_id="",
            assigner_name="",
            reason="Opening a new Change Request form is refused.",
            writes=writes,
            output_id=None,
            replayed=False,
        )
    if packet.get("ui_stuck") is True:
        writes.refuse("ui_rescue")

    writeback = _writeback_problem(packet)
    if writeback:
        filing = packet.get("filing") if isinstance(packet.get("filing"), Mapping) else {}
        return _finish(
            packet,
            outcomes=["writeback_unverified"],
            comparison=[],
            comparison_complete=False,
            assigner_id="",
            assigner_name="",
            reason=writeback,
            writes=writes,
            output_id=None,
            replayed=False,
            blocked_target=str(filing.get("document_id") or ""),
        )

    case = packet.get("case") if isinstance(packet.get("case"), Mapping) else {}
    if str(case.get("carrier") or "").strip().casefold() != PROVISIONAL_CARRIER.casefold():
        return _finish(
            packet,
            outcomes=["retrieval_blocked"],
            comparison=[],
            comparison_complete=False,
            assigner_id="",
            assigner_name="",
            reason="Progressive is the provisional carrier. This run will not switch carriers.",
            writes=writes,
            output_id=None,
            replayed=False,
        )

    filed_source = ezlynx_filed_carrier_source(packet)
    if packet.get("retrieve_live") is True:
        proof = progressive_access_proof(packet.get("carrier_proof") if isinstance(packet.get("carrier_proof"), Mapping) else None)
        if not proof["confirmed"] and filed_source is None:
            return _finish(
                packet,
                outcomes=["retrieval_blocked"],
                comparison=[],
                comparison_complete=False,
                assigner_id="",
                assigner_name="",
                reason=proof["reason"],
                writes=writes,
                output_id=None,
                replayed=False,
            )

    destination_reason, assigner_id, assigner_name = _destination_failure(packet)
    if not destination_reason:
        event = _assignment_event(packet)
        assert event is not None
        reread_problem = _reread_problem(packet, event)
        if reread_problem:
            return _finish(
                packet,
                outcomes=["stale_context"],
                comparison=[],
                comparison_complete=False,
                assigner_id=assigner_id,
                assigner_name=assigner_name,
                reason=reread_problem,
                writes=writes,
                output_id=None,
                replayed=False,
            )
    if packet.get("ui_stuck") is True:
        return _finish(
            packet,
            outcomes=["retrieval_blocked"],
            comparison=[],
            comparison_complete=False,
            assigner_id=assigner_id,
            assigner_name=assigner_name,
            reason="The screen is stuck. Use the existing Gemini rescue. Do not open a new Change Request form.",
            writes=writes,
            output_id=None,
            replayed=False,
        )
    exclusion = _exclusion_outcome(packet)
    if exclusion:
        return _finish(
            packet,
            outcomes=[exclusion],
            comparison=[],
            comparison_complete=False,
            assigner_id=assigner_id,
            assigner_name=assigner_name,
            reason="This change is outside the read-only pilot.",
            writes=writes,
            output_id=None,
            replayed=False,
        )
    document_problem = _document_problem(packet)
    if document_problem:
        return _finish(
            packet,
            outcomes=[document_problem],
            comparison=[],
            comparison_complete=False,
            assigner_id=assigner_id,
            assigner_name=assigner_name,
            reason="Issued endorsement evidence is not ready.",
            writes=writes,
            output_id=None,
            replayed=False,
        )

    comparison = build_comparison(packet)
    outcomes: list[str] = []
    for row in comparison:
        verdict = row.get("verdict")
        if verdict in {"exact_match", "normalized_match", "not_applicable"}:
            continue
        if verdict == "unrequested_change" and row.get("coverage"):
            outcomes.append("coverage_review_required")
        elif verdict == "unrequested_change" and row.get("side") == "ezlynx":
            outcomes.append("ezlynx_correction_required")
        elif verdict == "unrequested_change":
            outcomes.append("carrier_correction_required")
        elif verdict == "mismatch":
            request_cell = row.get("request") or {}
            carrier_cell = row.get("carrier") or {}
            ez_cell = row.get("ezlynx") or {}
            request_raw = request_cell.get("normalized")
            carrier_raw = carrier_cell.get("normalized")
            ez_raw = ez_cell.get("normalized")
            if request_cell.get("state") == "present" and carrier_cell.get("state") == "present" and request_raw != carrier_raw:
                outcomes.append("carrier_correction_required")
            if carrier_cell.get("state") == "present" and ez_cell.get("state") == "present" and carrier_raw != ez_raw:
                outcomes.append("ezlynx_correction_required")
            if (
                request_cell.get("state") == "present"
                and ez_cell.get("state") == "present"
                and carrier_cell.get("state") != "present"
                and request_raw != ez_raw
            ):
                outcomes.append("ezlynx_correction_required")
        elif verdict == "unknown":
            outcomes.append("evidence_invalid")
        elif verdict == "missing":
            outcomes.append("evidence_invalid")
        else:
            outcomes.append("request_unclear")
    filing = packet.get("filing") if isinstance(packet.get("filing"), Mapping) else {}
    if filing.get("verified") is not True:
        outcomes.append("evidence_invalid")
    if destination_reason:
        outcomes.append("destination_unverified")
    if not outcomes:
        outcomes.append("ready_for_human_review")
    return _finish(
        packet,
        outcomes=outcomes,
        comparison=comparison,
        comparison_complete=True,
        assigner_id=assigner_id,
        assigner_name=assigner_name,
        reason=destination_reason,
        writes=writes,
        output_id=None if destination_reason else str(uuid.uuid4()),
        replayed=False,
    )


def run_confirmation(
    packet: Mapping[str, Any],
    *,
    ledger: ConfirmationLedger | None = None,
    writes: DisabledWrites | None = None,
    prior_checkpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare one supplied case. External writes stay at zero."""
    if ledger is None:
        ledger = ConfirmationLedger()
    if writes is None:
        writes = DisabledWrites()
    packet = copy.deepcopy(dict(packet))
    snapshot = packet.get("ezlynx_snapshot")
    if isinstance(snapshot, Mapping) and not isinstance(packet.get("ezlynx_read"), Mapping):
        packet = apply_ezlynx_read(packet, snapshot)
    key = case_key(packet)
    digest = source_hash(packet)
    existing = ledger.get(key)
    if existing is None and isinstance(prior_checkpoint, Mapping):
        if str(prior_checkpoint.get("case_key") or "") == key:
            existing = dict(prior_checkpoint)
    if existing is not None:
        if existing.get("source_hash") == digest and isinstance(existing.get("result"), Mapping):
            replay = dict(existing["result"])
            replay["replayed"] = True
            replay["external_writes"] = 0
            replay["writes_enabled"] = False
            replay["completed"] = False
            replay["live_test"] = "UNVERIFIED"
            return replay
        stopped = _finish(
            packet,
            outcomes=["stale_context"],
            comparison=[],
            comparison_complete=False,
            assigner_id="",
            assigner_name="",
            reason="The case changed. The earlier result was not reused.",
            writes=writes,
            output_id=None,
            replayed=False,
        )
        stopped["prior_output_id"] = existing.get("output_id")
        return stopped

    result = _evaluate(packet, writes)
    if result.get("output_id"):
        ledger.put_once(
            key,
            {
                "case_key": key,
                "source_hash": digest,
                "output_id": result["output_id"],
                "result": result,
            },
        )
    return result


class PolicyChangeConfirmationWorker:
    """Bounded worker. The Job Engine already owns lifecycle and evidence."""

    def __init__(
        self,
        store: Any | None = None,
        ledger: ConfirmationLedger | None = None,
        writes: DisabledWrites | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger or ConfirmationLedger()
        self._writes = writes or DisabledWrites()

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        del idempotency_key
        action = str(job.get("action_type") or JOB_TYPE)
        if action != JOB_TYPE:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error="This worker only checks an assigned policy-change confirmation.",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        payload = dict(job.get("payload") or {})
        prior = None
        if self._store is not None and job.get("id"):
            prior = self._store.get_checkpoint(job["id"], CHECKPOINT)
        result = run_confirmation(
            payload,
            ledger=self._ledger,
            writes=self._writes,
            prior_checkpoint=prior,
        )
        if self._store is not None and job.get("id") and result.get("output_id"):
            self._store.checkpoint(
                job["id"],
                CHECKPOINT,
                {
                    "case_key": result.get("case_key"),
                    "source_hash": result.get("source_hash"),
                    "output_id": result.get("output_id"),
                    "result": result,
                },
            )
        hold = _HOLD_STATUS.get(str(result.get("outcome")), JobStatus.WAITING)
        return WorkerResult(
            False,
            action,
            {
                "task_open": True,
                "change_request_open": True,
                "external_writes": 0,
                "confirmation_owner": result.get("confirmation_owner"),
            },
            detail=result,
            retryable=False,
            error=str(result.get("result_sentence") or "The check is waiting."),
            hold_status=hold,
        )


class PolicyChangeConfirmationVerifier:
    """Refuses COMPLETE. The producer still confirms and closes."""

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        del job
        detail = dict(action.get("detail") or {})
        writes = int(detail.get("external_writes") or 0)
        observed = {
            "task_open": True,
            "change_request_open": True,
            "external_writes": writes,
            "status": "waiting_for_producer",
        }
        evidence = VerificationEvidence(
            method="policy_change_confirmation_packet",
            source="supplied_case_read",
            expected={"task_open": True, "external_writes": 0, "status": "waiting_for_producer"},
            observed=observed,
            authoritative=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=str(detail.get("case_key") or ""),
        )
        if writes != 0:
            return VerificationResult(
                False,
                evidence,
                retryable=False,
                error="A write was counted. This pilot cannot confirm it.",
                hold_status=JobStatus.WAITING,
            )
        return VerificationResult(
            False,
            evidence,
            retryable=False,
            error="The producer still has to confirm and close this. The task stays open.",
            hold_status=JobStatus.AWAITING_HUMAN_INPUT,
        )
