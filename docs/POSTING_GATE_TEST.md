# Posting Gate (Test-only, hard-gated)

`applied_pay/posting_gate.py` is the QBO/EZLynx posting path. It is
**hard-gated and never auto-posts**:

- The module contains **zero network calls** — no urllib, no requests, no
  SDK imports. It builds posting PLANS; it cannot execute them.
- Default every item to `dry_run_review_only`. The CLI never accepts
  approvals.
- An item becomes `approved_pending_manual_execution` only with a valid
  approval token naming the **exact payment**: amount (positive, exact
  cents), payee, policy — plus `approved_by` and an unexpired
  timezone-aware `expires_at`. Any mismatch, expiry, or missing field fails
  closed (item stays dry-run, or the token is rejected outright).
- Each token is **single-use** within a plan: one token authorizes one
  payment. Two identical items need two tokens.
- `transfer_allowed` is **always False** in this module. Execution is a
  separate, explicit operator step outside this code.

## Approval token shape

```json
{"token_id": "TOK-1", "amount": "250.00", "payee": "Acme Trucking LLC",
 "policy": "POL-123", "approved_by": "Carlo Ferrara",
 "expires_at": "2026-10-04T13:00:00+00:00"}
```

No token may close a carrier-payment task, move money, or post without
Carlo's explicit approval of the exact payment — the token IS that
approval, and it names amount, payee, and policy literally.
