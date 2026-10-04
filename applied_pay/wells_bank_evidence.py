"""Read-only Wells capture adapter. No network, credentials, postings or writes.

The caller must supply an independently checked guest-view capture manifest and
normalized transaction detail. This module cannot authenticate an uploaded JSON
file or establish that a portal's "posted" label means finally cleared funds.
"""
from __future__ import annotations
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

TRUST_ACCOUNT = "10002 Trust Checking WF (3021)"
ALLOWED_HOSTS = {"www.wellsfargo.com", "connect.secure.wellsfargo.com"}
REF = re.compile(r"[A-Z0-9]{8,40}")


class EvidenceError(ValueError):
    pass


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvidenceError("invalid bank amount") from exc
    if not result.is_finite() or result != result.quantize(Decimal("0.01")):
        raise EvidenceError("amount must be finite and exact to cents")
    return result


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("invalid transaction date") from exc


def _capture(manifest, *, artifact_bytes, now):
    """Check capture integrity. These fields do not independently prove origin."""
    from urllib.parse import urlsplit
    url = urlsplit(manifest.get("source_url", ""))
    if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS or url.username or url.password:
        raise EvidenceError("not an approved Wells guest-view source")
    if manifest.get("source_kind") != "wells_guest_transaction_detail" or manifest.get("access_mode") != "view_only":
        raise EvidenceError("Wells guest transaction-detail evidence required")
    if manifest.get("account_last4") != "3021":
        raise EvidenceError("wrong or unverified bank account")
    # An operator must establish the status semantics using the actual bank
    # detail, not infer clearing from a balance, email, QBO feed or label alone.
    if manifest.get("clearing_semantics_verified") is not True:
        raise EvidenceError("Wells clearing semantics not verified")
    if not manifest.get("review_reference") or not manifest.get("capture_id"):
        raise EvidenceError("independent capture review reference required")
    expected = manifest.get("artifact_sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or hashlib.sha256(artifact_bytes).hexdigest() != expected:
        raise EvidenceError("bank capture artifact hash mismatch")
    try:
        captured = datetime.fromisoformat(manifest["captured_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("capture timestamp required") from exc
    if captured.tzinfo is None or now.tzinfo is None:
        raise EvidenceError("timezone-aware capture and current timestamps required")
    age = now - captured
    if age < timedelta(0) or age > timedelta(hours=24):
        raise EvidenceError("bank capture stale or from the future")
    return captured


def compare(payouts, records, manifest, *, artifact_bytes, now):
    """Return cleared_bank_deposits and review findings, never silently skip.

    Input rows: bank_transaction_id, account_last4, posted_date, amount,
    direction, status, descriptor, transfer_ref. transfer_ref must be extracted
    literally from bank detail, not supplied from a guessed amount/date match.
    Strict one-payout/one-bank-item binding. All conflicts are held for review.
    """
    _capture(manifest, artifact_bytes=artifact_bytes, now=now)
    payout_refs = [p.get("ref") for p in payouts]
    if any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in payout_refs) or len(set(payout_refs)) != len(payout_refs):
        raise EvidenceError("duplicate or invalid payout reference")
    ids = [r.get("bank_transaction_id") for r in records]
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise EvidenceError("duplicate or missing bank transaction identifier")
    bank_refs = Counter(r.get("transfer_ref") for r in records if r.get("transfer_ref"))
    cleared, findings, used = [], [], set()
    for payout in payouts:
        ref = payout["ref"]
        if payout.get("lines_tie") is not True:
            findings.append({"transfer_ref":ref, "status":"stop", "reason":"Applied settlement lines do not tie"})
            continue
        amount, pday = money(payout["net"]), _date(payout["payout_date"])
        matches = [r for r in records if r.get("transfer_ref") == ref]
        if not matches:
            findings.append({"transfer_ref":ref,"status":"needs_review", "reason":"no literal Wells transfer-reference match; amount/date alone not proof"})
            continue
        if bank_refs[ref] != 1:
            findings.append({"transfer_ref":ref,"status":"stop","reason":"multiple Wells records for this transfer, including possible reversal"})
            continue
        row = matches[0]; reasons = []
        if row.get("account_last4") != "3021": reasons.append("wrong bank account")
        if row.get("status") != "posted_cleared": reasons.append("bank transaction not independently verified posted and cleared")
        if row.get("direction") != "credit" or amount <= 0: reasons.append("not a positive settlement credit")
        if money(row.get("amount")) != amount: reasons.append("bank amount differs from settlement")
        if not pday <= _date(row.get("posted_date")) <= pday + timedelta(days=1): reasons.append("bank date outside current matcher payout/next-day window")
        # A transfer can be supplied only if present as a complete token in the
        # literal bank descriptor, not as a substring of another identifier.
        tokens = re.findall(r"[A-Z0-9]+", row.get("descriptor", "").upper())
        if ref not in tokens: reasons.append("reference not literal in bank descriptor")
        if reasons:
            findings.append({"transfer_ref":ref,"status":"stop","bank_transaction_id":row["bank_transaction_id"],"reason":"; ".join(reasons)})
            continue
        tid = row["bank_transaction_id"]
        used.add(tid)
        cleared.append({"id":"WELLS-"+tid, "bank_transaction_id":tid, "date":row["posted_date"],
                        "amount":str(amount.quantize(Decimal("0.01"))), "account":TRUST_ACCOUNT,
                        "status":"cleared", "verified_payout_ref":ref, "verification_source":"bank_record",
                        "capture_id":manifest["capture_id"], "artifact_sha256":manifest["artifact_sha256"],
                        "review_reference":manifest["review_reference"], "source_url":manifest["source_url"]})
        findings.append({"transfer_ref":ref,"status":"bank_bound", "bank_transaction_id":tid,
                         "reason":"bank credit bound; QBO receipts and EZLynx fee/payable evidence still required"})
    unrelated = [r["bank_transaction_id"] for r in records if r["bank_transaction_id"] not in used]
    return {"cleared_bank_deposits":cleared, "findings":findings, "unbound_bank_transaction_ids":unrelated,
            "posts_made":0, "notes_written":0, "bank_actions":0}


def main():
    """Local captured-evidence CLI only; no live bank login or scraping."""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payouts", required=True, type=Path)
    parser.add_argument("--records", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    args = parser.parse_args()
    result = compare(json.loads(args.payouts.read_text()), json.loads(args.records.read_text()),
                     json.loads(args.manifest.read_text()), artifact_bytes=args.artifact.read_bytes(),
                     now=datetime.now(timezone.utc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
