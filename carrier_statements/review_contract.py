"""Private, supplied-source statement review. No network, writes, or payment decisions."""
from __future__ import annotations
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import re


def validate_source(source: dict, *, agency_id: str, month: str) -> list[str]:
    problems = []
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        problems.append("invalid_review_month")
    for key in ("agency_id", "carrier_id", "account_no", "statement_date", "printed_period", "statement_class", "parsed_carrier", "currency", "path", "sha256", "source_id"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            problems.append("missing_source_" + key)
    if source.get("agency_id") != agency_id:
        problems.append("source_agency_mismatch")
    try:
        date.fromisoformat(source.get("statement_date", ""))
    except (TypeError, ValueError):
        problems.append("invalid_statement_date")
    if source.get("currency") != "USD":
        problems.append("unsupported_or_unverified_currency")
    if source.get("printed_period") != month:
        problems.append("statement_month_mismatch")
    if source.get("statement_class") not in ("agency_payable", "commission_receivable", "finance_agency_balance"):
        problems.append("not_monthly_agency_statement")
    path = Path(source.get("path", ""))
    if not path.is_absolute():
        problems.append("source_path_not_absolute")
    elif not path.is_file():
        problems.append("source_file_unavailable")
    elif sha256(path.read_bytes()).hexdigest() != source.get("sha256"):
        problems.append("source_digest_mismatch")
    return sorted(set(problems))


def statement_holds(stmt: dict, source: dict, *, agency_id: str, month: str) -> list[str]:
    holds = validate_source(source, agency_id=agency_id, month=month)
    if not stmt.get("ties"):
        holds.append("statement_arithmetic_not_tied")
    if stmt.get("carrier") != source.get("parsed_carrier"):
        holds.append("parsed_carrier_mismatch")
    if stmt.get("account_no") != source.get("account_no"):
        holds.append("parsed_account_mismatch")
    if stmt.get("statement_date") != source.get("statement_date"):
        holds.append("parsed_statement_date_mismatch")
    if not stmt.get("lines") and stmt.get("total_due") != Decimal("0"):
        holds.append("empty_nonzero_statement")
    for row in stmt.get("lines", []):
        if row.get("net") is None:
            holds.append("missing_line_net")
        if row.get("txn_type") == "NOT_ITEMIZED":
            holds.append("unitemized_balance_difference")
        if any("quarantine" in str(flag).lower() for flag in row.get("flags", [])):
            holds.append("quarantined_credit")
    return sorted(set(holds))


def cents(value):
    """No floats, implicit zero, rounding, NaN, or infinity in financial comparisons."""
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("missing_or_inexact_amount")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_amount") from exc
    if not d.is_finite() or d * 100 != (d * 100).to_integral_value():
        raise ValueError("invalid_cent_precision")
    return int(d * 100)


def reconcile(stmt: dict, source: dict, records: list[dict], *, agency_id: str, month: str) -> dict:
    """Match invoices only. Even matched records are review-only, never cleared/pay-ready.

    Supplied records must carry stable record_id, agency_id, carrier_id, account_no,
    exact policy and invoice, and signed net. Names and amounts alone never bind.
    Financial funding/receipts/credits are separate evidence, not inferred here.
    """
    holds = statement_holds(stmt, source, agency_id=agency_id, month=month)
    report = {"agency_id": agency_id, "month": month, "source_id": source.get("source_id"),
              "source_sha256": source.get("sha256"), "holds": holds, "lines": [],
              "status": "HELD" if holds else "REVIEW_ONLY", "financial_reconciled": False,
              "pay_ready": False, "writes": 0}
    seen_lines = set()
    for index, line in enumerate(stmt.get("lines", [])):
        item = {"index": index, "status": "UNMATCHED", "record_id": None, "exceptions": []}
        key = (agency_id, source.get("carrier_id"), source.get("account_no"), source.get("statement_class"), source.get("currency"), line.get("policy"), line.get("invoice"))
        duplicate_key = (line.get("policy"), line.get("invoice"), line.get("net"), line.get("txn_type"), line.get("description"))
        if duplicate_key in seen_lines:
            item["exceptions"].append("duplicate_statement_line")
        seen_lines.add(duplicate_key)
        if holds:
            item["exceptions"].append("statement_held_no_match")
        elif item["exceptions"]:
            pass
        elif not line.get("policy") or not line.get("invoice"):
            item["exceptions"].append("missing_policy_or_invoice")
        elif line.get("txn_type") == "NOT_ITEMIZED":
            item["exceptions"].append("derived_line_not_invoice_evidence")
        else:
            candidates = [r for r in records if tuple(r.get(k) for k in ("agency_id", "carrier_id", "account_no", "statement_class", "currency", "policy", "invoice")) == key]
            if not candidates:
                item["exceptions"].append("invoice_not_found")
            elif len(candidates) != 1:
                item["exceptions"].append("invoice_ambiguous")
            else:
                record = candidates[0]
                if not record.get("record_id"):
                    item["exceptions"].append("missing_destination_record_id")
                try:
                    equal = cents(line.get("net")) == cents(record.get("net"))
                    if not equal:
                        item["exceptions"].append("signed_amount_mismatch")
                except ValueError:
                    item["exceptions"].append("invalid_amount")
                if not item["exceptions"]:
                    item.update(status="CANDIDATE_REVIEW_ONLY", record_id=record["record_id"])
        if line.get("kind") in ("credit", "payment", "adjustment"):
            item["exceptions"].append("allocation_owner_unverified")
        item["exceptions"].append("funding_receipts_bank_unverified")
        report["lines"].append(item)
    return report
