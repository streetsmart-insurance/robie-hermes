"""Map Ascend events to their destination bindings. Read-only.

Applicant (EZLynx): exact policy-number match through PolicyApi rows,
the same rule as the notice driver's ``resolve_applicant``. Every row for
that number must name the same applicant, or the event stays unmatched.
Insured-name matching is not used: a name hit can be the wrong account.
The task assignee is the CSR login from the Ascend program's producer (the
same ``resolve_cancellation_csr`` rule as the notice driver; PolicyApi rows
carry no CSR field), and it must have a confirmed numeric EZLynx user id
(direct Task API); otherwise the event stays unmatched instead of creating
a task nobody owns.

Payout (QBO): realm from the QuickBooks config, deposit/income accounts and
payee from configuration (names below, values never in code), amount and
currency from the Ascend payout. ``verify_qbo_mapping`` reads those ids
back from QBO before anything is allowed to send.

Nothing here writes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .ezlynx_user_ids import ezlynx_user_id_for

logger = logging.getLogger(__name__)

QBO_DEPOSIT_ACCOUNT_ENV = "ROBIE_QBO_ASCEND_DEPOSIT_ACCOUNT_ID"
QBO_INCOME_ACCOUNT_ENV = "ROBIE_QBO_ASCEND_COMMISSION_INCOME_ACCOUNT_ID"
# "Vendor:<id>" or "Customer:<id>". QBO Vendor and Customer ids overlap, so a
# bare id can name the wrong record; it is refused.
QBO_PAYEE_ENV = "ROBIE_QBO_ASCEND_PAYEE"
QBO_PAYEE_TYPES = {"vendor": "Vendor", "customer": "Customer"}
ACCOUNTING_ASSIGNEE_ENV = "ROBIE_ASCEND_ACCOUNTING_ASSIGNEE"

APPLICANT_KINDS = ("cancellation", "agreement_signed", "accounting_issue")


def map_applicant(event: dict[str, Any], ezlynx_client: Any) -> tuple[dict[str, Any], str]:
    """Applicant fields for an event, or ({}, reason). Policy number only."""
    from .ascend_notice_driver import (
        _APPLICANT_ID_KEYS,
        _first_present,
        _policy_rows,
        _row_policy_number,
        resolve_cancellation_csr,
    )

    number = str(event.get("policy_number") or "").strip()
    if not number:
        return {}, "unmatched_no_policy_number"
    if ezlynx_client is None:
        return {}, "unmatched_no_policy_client"
    try:
        result = ezlynx_client.search_policy_by_number(number)
    except Exception as exc:  # noqa: BLE001 - lookup failure is retryable
        return {}, f"unmatched_policy_search_failed_{type(exc).__name__}"
    rows = [r for r in _policy_rows(result) if _row_policy_number(r).casefold() == number.casefold()]
    applicants = {_first_present(r, _APPLICANT_ID_KEYS) for r in rows} - {""}
    if not applicants:
        return {}, "unmatched_policy_not_found"
    if len(applicants) > 1:
        return {}, "unmatched_policy_ambiguous"
    applicant_id = applicants.pop()

    if event.get("kind") == "accounting_issue":
        assignee = os.getenv(ACCOUNTING_ASSIGNEE_ENV, "").strip()
    else:
        # CSR is the Ascend program producer (PolicyApi rows carry no CSR
        # field) — the same resolve_cancellation_csr rule as the notice
        # driver, so both paths assign the same CSR.
        assignee, csr_reason = resolve_cancellation_csr(event.get("program"))
        if not assignee:
            logger.debug("map_applicant: %s", csr_reason)
    if not assignee:
        return {}, "unmatched_no_assignee_login"
    user_id = ezlynx_user_id_for(assignee)
    if user_id is None:
        return {}, "unmatched_assignee_id_unconfirmed"
    return {
        "applicant_id": applicant_id,
        "assignee": assignee,
        "assignee_user_id": user_id,
    }, ""


def parse_payee(value: str | None) -> tuple[str, str, str]:
    """("Vendor"|"Customer", id, "") or ("", "", reason). Bare ids are refused."""
    text = str(value or "").strip()
    if not text:
        return "", "", f"{QBO_PAYEE_ENV} unset"
    kind, sep, ident = text.partition(":")
    entity = QBO_PAYEE_TYPES.get(kind.strip().casefold())
    ident = ident.strip()
    if not sep or entity is None:
        return "", "", f"{QBO_PAYEE_ENV} must be Vendor:<id> or Customer:<id>, not a bare id"
    if not ident.isdigit():
        return "", "", f"{QBO_PAYEE_ENV} id must be numeric"
    return entity, ident, ""


def map_payout(event: dict[str, Any], realm_id: Optional[str]) -> tuple[dict[str, Any], str]:
    """QBO binding for a commission payout, or ({}, reason)."""
    payee_type, payee_id, payee_why = parse_payee(os.getenv(QBO_PAYEE_ENV))
    if payee_why and os.getenv(QBO_PAYEE_ENV, "").strip():
        return {}, "payout_payee_setting_invalid"
    binding = {
        "realm_id": str(realm_id or "").strip(),
        "account_id": os.getenv(QBO_DEPOSIT_ACCOUNT_ENV, "").strip(),
        "income_account_id": os.getenv(QBO_INCOME_ACCOUNT_ENV, "").strip(),
        "payee_type": payee_type,
        "payee_id": payee_id,
        "currency": str(event.get("currency") or "").strip().upper(),
    }
    try:
        amount = int(event.get("amount_cents"))
    except (TypeError, ValueError):
        return {}, "payout_amount_missing"
    if amount <= 0:
        return {}, "payout_amount_not_positive"
    binding["amount_cents"] = amount
    missing = [k for k, v in binding.items() if v in ("", None)]
    if missing:
        return {}, "payout_binding_missing_" + "_".join(sorted(missing))
    return binding, ""


def map_event(event: dict[str, Any], *, ezlynx_client: Any = None, realm_id: Optional[str] = None) -> dict[str, Any]:
    """Return a copy of ``event`` with its binding, plus ``mapping_reason``."""
    mapped = dict(event)
    kind = event.get("kind")
    if kind in APPLICANT_KINDS:
        fields, reason = map_applicant(event, ezlynx_client)
    elif kind == "commission_payout":
        fields, reason = map_payout(event, realm_id)
    else:
        fields, reason = {}, ""
    mapped.update(fields)
    mapped["mapping_reason"] = reason
    return mapped


def verify_qbo_mapping(qb: Any) -> dict[str, Any]:
    """Read the configured QBO ids back. Read-only. Returns per-id findings.

    Deposit account must be an active Bank account, income account an
    active Income account, and the payee an active record of exactly the
    entity type named in the setting.
    """
    findings: dict[str, Any] = {"ok": False, "realm_id": getattr(qb.config, "realm_id", None)}
    checks = (
        ("deposit_account", QBO_DEPOSIT_ACCOUNT_ENV, "account", ("Bank",)),
        ("income_account", QBO_INCOME_ACCOUNT_ENV, "account", ("Income", "Other Income")),
    )
    all_ok = True
    for label, env, resource, types in checks:
        value = os.getenv(env, "").strip()
        if not value:
            findings[label] = {"ok": False, "reason": f"{env} unset"}
            all_ok = False
            continue
        try:
            record = qb._request("GET", f"{resource}/{value}").get("Account") or {}
        except Exception as exc:  # noqa: BLE001
            findings[label] = {"ok": False, "reason": type(exc).__name__}
            all_ok = False
            continue
        ok = str(record.get("Id")) == value and record.get("Active") is True and record.get("AccountType") in types
        findings[label] = {"ok": ok, "id": record.get("Id"), "type": record.get("AccountType"),
                           "active": record.get("Active"), "name": record.get("Name")}
        all_ok = all_ok and ok
    found = None
    entity, payee, why = parse_payee(os.getenv(QBO_PAYEE_ENV))
    if why:
        found = {"ok": False, "reason": why}
    else:
        # Only the configured entity type: a Vendor id that also exists as a
        # Customer must never be accepted as that Customer, or vice versa.
        try:
            record = qb._request("GET", f"{entity.lower()}/{payee}").get(entity) or {}
        except Exception as exc:  # noqa: BLE001
            found = {"ok": False, "type": entity, "reason": type(exc).__name__}
        else:
            ok = str(record.get("Id")) == payee and record.get("Active") is True
            found = {"ok": ok, "type": entity, "id": record.get("Id"),
                     "active": record.get("Active"), "name": record.get("DisplayName")}
    findings["payee"] = found
    findings["ok"] = all_ok and bool(found and found["ok"])
    return findings
