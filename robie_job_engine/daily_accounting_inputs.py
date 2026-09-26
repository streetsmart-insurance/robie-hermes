"""Fail-closed import of read-only, independently captured source exports.

These adapters do not declare a source complete from an email, a CSV, or one
API page. Exports must carry the collector's pagination/scope attestation.
The collectors themselves are a separate integration step, not enabled here.
"""
from __future__ import annotations
import json
from pathlib import Path
from .daily_accounting_checks import Evidence, SourceSnapshot

ASCEND_COLLECTIONS = ("cancelation_returns", "invoices", "programs", "loans", "payouts")


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"_error": type(exc).__name__}


def _incomplete(source, error):
    return SourceSnapshot(source, [], False, error=error)


def load_ascend_export(path):
    source = "Ascend"; data = _read(path)
    if data.get("_error"):
        return _incomplete(source, data["_error"])
    collections = data.get("collections", {})
    if not data.get("as_of") or any(not collections.get(k, {}).get("exhausted") for k in ASCEND_COLLECTIONS):
        return _incomplete(source, "missing collection, as-of time, or verified pagination exhaustion")
    items = []
    try:
        for name in ASCEND_COLLECTIONS:
            for record in collections[name]["records"]:
                rid = str(record["id"])
                if not rid or not record.get("updated_at"):
                    raise ValueError("missing Ascend ID/update time")
                fields = ("status", "payment_status", "amount_cents", "due_date", "effective_date",
                          "downpayment_cents", "payout_amount_cents", "finance_agreement_status")
                for field in fields:
                    if field not in record:
                        continue
                    items.append(Evidence(source, f"{name}:{rid}", data["as_of"],
                      record["updated_at"], str(record.get("amount_cents", "")), "USD",
                      str(record[field]), str(record.get("account_id", "")), field,
                      detail=f"{name} record {rid}"))
    except (KeyError, TypeError, ValueError) as exc:
        return _incomplete(source, f"invalid record: {type(exc).__name__}")
    return SourceSnapshot(source, items, True, as_of=data["as_of"], provenance=str(path))


def load_applied_batches(path):
    source = "Applied Pay"; data = _read(path)
    if data.get("_error"):
        return _incomplete(source, data["_error"])
    if not data.get("complete") or not data.get("scope") or not data.get("as_of"):
        return _incomplete(source, "missing full-scope batch coverage attestation")
    items = []
    try:
        for batch in data["batches"]:
            for line in batch["lines"]:
                typ = str(line["type"]).lower()
                if typ not in ("refund", "return", "ach_chargeback", "card_chargeback", "chargeback", "reversal"):
                    continue
                items.append(Evidence(source, f'{batch["ref"]}:{line["psp_ref"]}', data["as_of"],
                   line["txn_date"], line["amount"], "USD", typ,
                   str(line.get("policy") or ""), "return", detail=f'batch {batch["ref"]}'))
    except (KeyError, TypeError, ValueError) as exc:
        return _incomplete(source, f"invalid return line: {type(exc).__name__}")
    return SourceSnapshot(source, items, True, as_of=data["as_of"], provenance=str(path))


def load_ezlynx_tasks(path):
    source = "EZLynx Accounting Team"; data = _read(path)
    if data.get("_error"):
        return _incomplete(source, data["_error"])
    if not data.get("complete") or data.get("scope") != "all Accounting Team open tasks" or not data.get("as_of"):
        return _incomplete(source, "missing complete open-task scope attestation")
    items = []
    try:
        for task in data["tasks"]:
            if task["assigned_team"] != "Accounting Team" or task["status"].lower() in ("closed", "complete", "done"):
                continue
            if not task.get("due_date") or not task.get("id"):
                raise ValueError("open task lacks ID or due date")
            items.append(Evidence(source, str(task["id"]), data["as_of"],
              str(task.get("updated_at") or data["as_of"]), str(task.get("amount") or ""), "USD",
              task["status"], str(task.get("applicant_id") or ""), "open_task",
              due_date=task["due_date"], detail=str(task.get("title") or "")))
    except (KeyError, TypeError, ValueError) as exc:
        return _incomplete(source, f"invalid task: {type(exc).__name__}")
    return SourceSnapshot(source, items, True, as_of=data["as_of"], provenance=str(path))
