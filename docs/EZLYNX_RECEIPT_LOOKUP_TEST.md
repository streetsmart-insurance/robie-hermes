# EZLynx Receipt Lookup (Test-only)

`applied_pay/ezlynx_receipt_lookup.py` matches Applied Pay payouts to EZLynx
receipts: receipt number, amount, applied status, invoice number.
Read-only: no EZLynx calls, no browser automation, no writes.

## Input contract

Receipts are plain JSON snapshots pulled **elsewhere via the EZLynx API read
surface** (never browser automation, per the repo's API-only rule for EZLynx):

```json
[{"receipt_number": "R-1", "amount": "250.00", "applied_status": "applied",
  "invoice_number": "INV-1001", "memo": "PAYOUT SYNREF001 SETTLED",
  "source": "ezlynx_api"}]
```

Payouts: `[{"ref": "SYNREF001", "net": "250.00", "payout_date": "2026-10-01",
"invoice_number": "INV-1001"}]`.

## Binding rules (strict, two-sided)

A receipt binds to a payout only when ALL hold:

1. Exact amount match (Decimal, exact cents).
2. `applied_status` in `{"applied", "posted"}` — void/unapplied/pending are held.
3. An independent binding: **invoice number match** OR the payout reference
   as a **literal token** in the receipt memo (substring of a longer token
   does not bind).

Amount alone never binds. Duplicate payout refs or receipt numbers fail
closed. Multiple bound receipts for one payout are flagged for review,
never auto-picked.

## Output

Per payout: `receipt_bound` (with receipt number, binding basis, and
`verification_source: "ezlynx_api_snapshot"`) or `needs_review` with
reasons. Unmatched receipts are listed. All write counters are zero.
