"""Hard-gated QBO/EZLynx posting path. Default dry-run/review-only.

This module NEVER performs a post: it contains zero network calls, zero SDK
imports, and zero write paths. It builds posting PLANS. A plan item moves
from 'dry_run_review_only' to 'approved_pending_manual_execution' only when
presented with a valid approval token naming the EXACT payment (amount,
payee, policy), signed by an approver, and unexpired. transfer_allowed is
always False in this module -- execution is a separate, explicit operator
step outside this code. Nothing here auto-posts, ever.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

DRY_RUN = "dry_run_review_only"
APPROVED = "approved_pending_manual_execution"


class PostingGateError(ValueError):
    pass


def money(value):
    if isinstance(value, bool):
        raise PostingGateError("invalid money")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PostingGateError("invalid amount") from exc
    if not result.is_finite() or result <= 0 or result != result.quantize(Decimal("0.01")):
        raise PostingGateError("amount must be positive and exact to cents")
    return result


def _utcnow(now):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise PostingGateError("timezone-aware now required")
    return now


def _validate_token(token, now):
    """A token must name one exact payment: amount, payee, policy."""
    for field in ("token_id", "amount", "payee", "policy", "approved_by", "expires_at"):
        if not token.get(field):
            raise PostingGateError("approval token missing %s" % field)
    amount = money(token["amount"])
    try:
        expires = datetime.fromisoformat(token["expires_at"])
    except (TypeError, ValueError) as exc:
        raise PostingGateError("approval token expiry invalid") from exc
    if expires.tzinfo is None:
        raise PostingGateError("approval token expiry must be timezone-aware")
    if expires <= now:
        raise PostingGateError("approval token expired")
    return {"token_id": str(token["token_id"]), "amount": amount,
            "payee": str(token["payee"]), "policy": str(token["policy"]),
            "approved_by": str(token["approved_by"])}


def build_plan(items, *, approvals=(), environment="TEST", now=None):
    """Build a posting plan. Default every item to dry-run/review-only.

    items: [{kind: 'qbo_deposit'|'ezlynx_note', amount, payee, policy, ...}].
    approvals: iterable of approval-token dicts, each naming exactly one
        payment (amount, payee, policy). A token is single-use within a plan.
    """
    if environment != "TEST":
        raise PostingGateError("Test only")
    now = _utcnow(now)
    valid = []
    seen_ids = set()
    for token in approvals:
        checked = _validate_token(token, now)
        if checked["token_id"] in seen_ids:
            raise PostingGateError("duplicate approval token id")
        seen_ids.add(checked["token_id"])
        valid.append(checked)

    planned, used_tokens = [], set()
    for item in items:
        kind = item.get("kind")
        if kind not in ("qbo_deposit", "ezlynx_note"):
            raise PostingGateError("unsupported posting kind: %r" % (kind,))
        amount = money(item.get("amount"))
        payee = str(item.get("payee") or "")
        policy = str(item.get("policy") or "")
        if not payee or not policy:
            raise PostingGateError("posting item must name payee and policy")
        entry = {**item, "amount": str(amount), "status": DRY_RUN,
                 "approval_token_id": None,
                 "reasons": ["no approval token presented; review only"]}
        for token in valid:
            if token["token_id"] in used_tokens:
                continue
            if (token["amount"] == amount and token["payee"] == payee
                    and token["policy"] == policy):
                used_tokens.add(token["token_id"])
                entry["status"] = APPROVED
                entry["approval_token_id"] = token["token_id"]
                entry["approved_by"] = token["approved_by"]
                entry["reasons"] = ["exact payment approved by %s; awaiting manual execution"
                                    % token["approved_by"]]
                break
        else:
            if valid:
                entry["reasons"] = ["no approval token names this exact payment (amount, payee, policy)"]
        planned.append(entry)
    return {"mode": "posting_gate", "environment": environment,
            "as_of": now.isoformat(),
            "default": DRY_RUN,
            "items": planned,
            "transfer_allowed": False,
            "posts_made": 0, "bank_actions": 0,
            "qbo_posts": 0, "ezlynx_writes": 0, "notes_written": 0}


def main():
    """CLI: build a dry-run plan from JSON items. Approvals are never accepted via CLI."""
    import argparse
    from pathlib import Path
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    plan = build_plan(json.loads(args.items.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"default": plan["default"],
                      "items": len(plan["items"]),
                      "approved": sum(1 for i in plan["items"] if i["status"] == APPROVED),
                      "transfer_allowed": plan["transfer_allowed"],
                      "out": str(args.out)}, indent=2))


if __name__ == "__main__":
    main()
