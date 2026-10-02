"""Read-only extract of an EZLynx policy-change snapshot.

EZLynx has no Task API. A token request for ``TaskApi`` returns
``400 invalid_scope`` (confirmed 2026-09-27). This module does not call
EZLynx. It reads a snapshot the Test session already fetched: a task row,
policy vehicles, and an explicit change effective date.

It does not write, click, or invent a value the snapshot does not carry.
A policy-change transaction date is not the change effective date. A
discussion note is not submission evidence.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping


_TASK_ID_KEYS = ("task_id", "taskId", "TaskId", "TaskID", "id", "Id")
_DUE_KEYS = ("due_date", "dueDate", "DueDate", "Due")
_ASSIGNEE_ID_KEYS = (
    "assignee_id",
    "assigned_user_id",
    "AssignedUserId",
    "OwnerId",
    "owner_id",
)
_ASSIGNEE_NAME_KEYS = (
    "assignee_name",
    "assignee",
    "AssignedUserName",
    "AssignedUser",
    "assigned_user",
    "current_owner_name",
    "OwnerName",
)
_APPLICANT_KEYS = ("applicant_id", "ApplicantId", "applicantId", "ApplicantID")
_POLICY_NUMBER_KEYS = ("policy_number", "PolicyNumber", "policyNumber")
_POLICY_ID_KEYS = (
    "policy_id",
    "PolicyId",
    "policyId",
    "policyMasterId",
    "PolicyMasterID",
    "policyMasterID",
)
_SUBMISSION_KEYS = (
    "submission_evidence",
    "SubmissionEvidence",
    "carrier_confirmation",
    "CarrierConfirmation",
    "submission_receipt",
)
_CHANGE_EFFECTIVE_KEYS = (
    "change_effective_date",
    "ChangeEffectiveDate",
    "changeEffectiveDate",
    "PolicyChangeEffectiveDate",
    "policyChangeEffectiveDate",
)
_TRANSACTION_DATE_KEYS = (
    "transaction_date",
    "TransactionDate",
    "transactionDate",
    "processed_date",
    "ProcessedDate",
)
_VEHICLE_LIST_KEYS = ("vehicles", "Vehicles", "vehicleList", "VehicleList")
_YEAR_KEYS = ("year", "Year", "ModelYear")
_MAKE_KEYS = ("make", "Make")
_MODEL_KEYS = ("model", "Model")
_VIN_KEYS = ("vin", "VIN", "Vin")
_STATUS_KEYS = ("status", "Status", "vehicle_status")
_REMOVED = frozenset({"removed", "deleted", "delete", "inactive"})


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row and _text(row.get(key)):
            return _text(row.get(key))
    return ""


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _select_task(
    tasks: list[Mapping[str, Any]],
    *,
    applicant_id: str,
    policy_number: str,
    policy_id: str,
) -> tuple[Mapping[str, Any] | None, str]:
    if not tasks:
        return None, "no task row was in the snapshot"
    if len(tasks) == 1:
        task = tasks[0]
        number = _first(task, _POLICY_NUMBER_KEYS)
        master = _first(task, _POLICY_ID_KEYS)
        applicant = _first(task, _APPLICANT_KEYS)
        if policy_number and number and number != policy_number:
            return None, "the only task is for a different policy"
        if policy_id and master and master != policy_id:
            return None, "the only task is for a different policy"
        if applicant_id and applicant and applicant != applicant_id:
            return None, "the only task is for a different applicant"
        return task, ""
    matched: list[Mapping[str, Any]] = []
    for task in tasks:
        number = _first(task, _POLICY_NUMBER_KEYS)
        master = _first(task, _POLICY_ID_KEYS)
        applicant = _first(task, _APPLICANT_KEYS)
        if policy_number and number and number != policy_number:
            continue
        if policy_id and master and master != policy_id:
            continue
        if applicant_id and applicant and applicant != applicant_id:
            continue
        identity = (number and number == policy_number) or (master and master == policy_id)
        if identity or (applicant and applicant == applicant_id and not number and not master):
            matched.append(task)
    if len(matched) == 1:
        return matched[0], ""
    if len(matched) > 1:
        return None, "more than one task matches this policy"
    return None, "no single task matches this policy"


def _submission_evidence(snapshot: Mapping[str, Any], task: Mapping[str, Any] | None) -> str:
    direct = _text(snapshot.get("submission_evidence"))
    if direct:
        return direct
    if task is not None:
        found = _first(task, _SUBMISSION_KEYS)
        if found:
            return found
    documents = _rows(snapshot.get("documents"))
    receipts = []
    for document in documents:
        kind = _text(document.get("kind") or document.get("type") or document.get("Type")).casefold()
        if kind not in {"submission_receipt", "submission_confirmation"}:
            continue
        name = _text(document.get("name") or document.get("Name"))
        document_id = _text(document.get("id") or document.get("document_id") or document.get("DocumentId"))
        receipts.append(" ".join(part for part in (name, document_id) if part))
    if len(receipts) == 1:
        return receipts[0]
    return ""


def _policy_block(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = snapshot.get("policy")
    if isinstance(policy, Mapping):
        return policy
    return snapshot


def _change_effective_date(policy: Mapping[str, Any]) -> str:
    found = _first(policy, _CHANGE_EFFECTIVE_KEYS)
    if found:
        return found
    transactions = _rows(policy.get("transactions") or policy.get("Transactions"))
    dated = []
    for row in transactions:
        value = _first(row, _CHANGE_EFFECTIVE_KEYS)
        if value:
            dated.append(value)
    if len(dated) == 1:
        return dated[0]
    return ""


def _transaction_date(policy: Mapping[str, Any]) -> str:
    transactions = _rows(policy.get("transactions") or policy.get("Transactions"))
    dates = []
    for row in transactions:
        value = _first(row, _TRANSACTION_DATE_KEYS)
        if value:
            dates.append(value)
    if len(dates) == 1:
        return dates[0]
    return _first(policy, _TRANSACTION_DATE_KEYS)


def _vehicles(policy: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_rows: list[Mapping[str, Any]] = []
    for key in _VEHICLE_LIST_KEYS:
        raw_rows = _rows(policy.get(key))
        if raw_rows:
            break
    vehicles = []
    for row in raw_rows:
        vehicles.append(
            {
                "year": _first(row, _YEAR_KEYS),
                "make": _first(row, _MAKE_KEYS),
                "model": _first(row, _MODEL_KEYS),
                "vin": _first(row, _VIN_KEYS).upper(),
                "status": _first(row, _STATUS_KEYS).casefold(),
                "source_id": _text(row.get("source_id") or row.get("id") or row.get("Id")),
            }
        )
    return vehicles


def read_policy_change_context(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract task, submission, vehicle, and change-effective fields.

    ``unread`` lists fields the snapshot did not carry. Nothing in this
    function opens a socket.
    """
    supplied = dict(snapshot or {})
    applicant_id = _text(supplied.get("applicant_id"))
    policy_number = _text(supplied.get("policy_number"))
    policy_id = _text(supplied.get("policy_id"))
    tasks = _rows(supplied.get("tasks") if "tasks" in supplied else supplied.get("Tasks"))
    task, task_gap = _select_task(
        tasks,
        applicant_id=applicant_id,
        policy_number=policy_number,
        policy_id=policy_id,
    )
    policy = _policy_block(supplied)
    vehicles = _vehicles(policy)
    change_effective = _change_effective_date(policy)
    transaction_date = _transaction_date(policy)
    submission = _submission_evidence(supplied, task)
    task_id = _first(task, _TASK_ID_KEYS) if task is not None else ""
    due_date = _first(task, _DUE_KEYS) if task is not None else ""
    assignee_id = _first(task, _ASSIGNEE_ID_KEYS) if task is not None else ""
    assignee_name = _first(task, _ASSIGNEE_NAME_KEYS) if task is not None else ""
    unread: list[str] = []
    if not task_id:
        unread.append("task_id")
    if not due_date:
        unread.append("due_date")
    if not assignee_id and not assignee_name:
        unread.append("assignee")
    if not submission:
        unread.append("submission_evidence")
    if not vehicles:
        unread.append("ezlynx_vehicle_list")
    if not change_effective:
        unread.append("ezlynx_change_effective_date")
    return {
        "task_id": task_id,
        "due_date": due_date,
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "submission_evidence": submission,
        "vehicles": vehicles,
        "change_effective_date": change_effective,
        "transaction_date": transaction_date,
        "transaction_date_used_as_change_effective": False,
        "task_gap": task_gap,
        "unread": unread,
        "writes": 0,
    }


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cited(raw: str, reference: str) -> dict[str, str]:
    return {"raw": raw, "reference": reference}


def apply_ezlynx_read(packet: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``packet`` and fill only fields the snapshot actually contains."""
    updated = copy.deepcopy(dict(packet))
    read = read_policy_change_context(snapshot)
    case = updated.get("case")
    if not isinstance(case, dict):
        case = {}
        updated["case"] = case
    task = updated.get("task")
    if not isinstance(task, dict):
        task = {}
        updated["task"] = task
    record = updated.get("ezlynx_record")
    if not isinstance(record, dict):
        record = {"fields": {}, "unrequested": []}
        updated["ezlynx_record"] = record
    fields = record.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        record["fields"] = fields

    if read["task_id"] and _blank(case.get("task_id")):
        case["task_id"] = read["task_id"]
        updated["task_id"] = read["task_id"]
        if _blank(task.get("id")):
            task["id"] = read["task_id"]
    if read["due_date"] and _blank(case.get("due_date")):
        case["due_date"] = read["due_date"]
    if read["submission_evidence"] and _blank(case.get("submission_evidence")):
        case["submission_evidence"] = read["submission_evidence"]
    if read["assignee_name"] and _blank(task.get("current_owner_id")):
        task["current_owner_id"] = read["assignee_name"]
    if read["assignee_id"] and _blank(task.get("assignee_id")):
        task["assignee_id"] = read["assignee_id"]
    if read["vehicles"] and not record.get("vehicles"):
        record["vehicles"] = list(read["vehicles"])
    if read["change_effective_date"] and "effective_date" not in fields:
        fields["effective_date"] = _cited(
            read["change_effective_date"],
            "EZLynx policy change effective date",
        )
    vin_cell = fields.get("vin")
    vin_blank = vin_cell is None or (isinstance(vin_cell, Mapping) and _blank(vin_cell.get("raw")))
    if vin_blank and read["vehicles"]:
        removed = [item for item in read["vehicles"] if item.get("status") in _REMOVED and item.get("vin")]
        chosen = removed if len(removed) == 1 else []
        if not chosen and len(read["vehicles"]) == 1 and read["vehicles"][0].get("vin"):
            chosen = read["vehicles"]
        if len(chosen) == 1:
            fields["vin"] = _cited(chosen[0]["vin"], "EZLynx vehicle list")
    if read["transaction_date"] and _blank(record.get("transaction_date")):
        record["transaction_date"] = read["transaction_date"]
    updated["ezlynx_read"] = read
    updated["ezlynx_snapshot"] = copy.deepcopy(dict(snapshot))
    return updated
