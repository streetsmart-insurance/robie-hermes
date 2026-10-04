"""Match Applied Pay payouts to EZLynx receipts. Read-only. No browser, no writes.

Receipts are plain JSON snapshots pulled elsewhere via the EZLynx API read
surface (DiscussionApi/PolicyApi read methods) -- never browser automation,
per the repo's EZLynx-notes-and-documents-are-API-only rule. This module
never calls EZLynx itself; it only correlates snapshots.

Binding is strict and two-sided: exact amount AND an applied/posted status
AND an independent binding (invoice number match or the payout reference as
a literal token in the receipt memo). Amount alone never binds.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import re

APPLIED_STATUSES = {"applied", "posted"}
TOKEN = re.compile(r"[A-Z0-9]+")


class ReceiptLookupError(ValueError):
    pass


def money(value):
    if isinstance(value, bool):
        raise ReceiptLookupError("invalid money")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReceiptLookupError("invalid receipt amount") from exc
    if not result.is_finite() or result != result.quantize(Decimal("0.01")):
        raise ReceiptLookupError("amount must be finite and exact to cents")
    return result


def _literal_token(ref, memo):
    """True only if ref appears as a complete token in the memo, not a substring."""
    if not ref or not memo:
        return False
    return ref.upper() in set(TOKEN.findall(str(memo).upper()))


def lookup(payouts, receipts, *, environment="TEST"):
    """Return per-payout receipt bindings plus review findings. Never writes."""
    if environment != "TEST":
        raise ReceiptLookupError("Test only")
    refs = [p.get("ref") for p in payouts]
    if any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
        raise ReceiptLookupError("duplicate or missing payout reference")
    rids = [r.get("receipt_number") for r in receipts]
    if any(not isinstance(i, str) or not i for i in rids) or len(set(rids)) != len(rids):
        raise ReceiptLookupError("duplicate or missing receipt number")

    results, used = [], set()
    for payout in payouts:
        ref = payout["ref"]
        amount = money(payout["net"])
        invoice = (payout.get("invoice_number") or "").strip()
        bound, reasons = [], []
        for receipt in receipts:
            try:
                ramount = money(receipt.get("amount"))
            except ReceiptLookupError:
                reasons.append({"receipt_number": receipt.get("receipt_number"),
                                "reason": "receipt amount invalid; skipped"})
                continue
            if ramount != amount:
                continue
            status = str(receipt.get("applied_status") or "").lower()
            if status not in APPLIED_STATUSES:
                reasons.append({"receipt_number": receipt.get("receipt_number"),
                                "reason": "receipt not applied/posted (status=%s)" % status})
                continue
            binds = []
            if invoice and (receipt.get("invoice_number") or "").strip() == invoice:
                binds.append("invoice_number")
            if _literal_token(ref, receipt.get("memo")):
                binds.append("payout_reference")
            if not binds:
                reasons.append({"receipt_number": receipt.get("receipt_number"),
                                "reason": "amount matches but no invoice or payout-reference binding; amount alone not proof"})
                continue
            bound.append(receipt)
        if len(bound) == 1:
            receipt = bound[0]
            used.add(receipt["receipt_number"])
            binds = []
            if invoice and (receipt.get("invoice_number") or "").strip() == invoice:
                binds.append("invoice_number")
            if _literal_token(ref, receipt.get("memo")):
                binds.append("payout_reference")
            results.append({"payout_ref": ref, "status": "receipt_bound",
                            "receipt_number": receipt["receipt_number"],
                            "amount": str(amount), "applied_status": receipt["applied_status"],
                            "invoice_number": receipt.get("invoice_number"),
                            "binding": "amount+status+" + "+".join(binds),
                            "verification_source": "ezlynx_api_snapshot",
                            "review_reasons": []})
        elif len(bound) > 1:
            results.append({"payout_ref": ref, "status": "needs_review",
                            "receipt_number": None,
                            "review_reasons": ["multiple EZLynx receipts bind to this payout"] +
                            [r["reason"] for r in reasons]})
        else:
            results.append({"payout_ref": ref, "status": "needs_review",
                            "receipt_number": None,
                            "review_reasons": (["no bound EZLynx receipt"] +
                                               [r["reason"] for r in reasons])})
    unmatched = [{"receipt_number": r["receipt_number"], "amount": str(r.get("amount")),
                  "applied_status": r.get("applied_status")}
                 for r in receipts if r["receipt_number"] not in used]
    return {"mode": "ezlynx_receipt_lookup", "environment": environment,
            "results": results, "unmatched_receipts": unmatched,
            "ezlynx_reads": 0, "ezlynx_writes": 0,
            "bank_actions": 0, "qbo_posts": 0, "notes_written": 0}
