# Wells Proof Job (Test-only)

`applied_pay/wells_proof_job.py` turns the captured-page reader
(`wells_guest_reader.py`) into a scheduled proof job. Read-only: no network,
no bank login, no QBO/EZLynx writes, no money movement. All write-surface
counters (`bank_actions`, `qbo_posts`, `ezlynx_writes`, `notes_written`,
`transfers`) are zero by construction.

## What it does

1. Ingests operator-supplied captured Wells guest pages for **Trust 3021**
   and **Operating 3018** (validated by the reader: https source, view-only,
   identity review reference, 24h freshness, artifact hash binding).
2. Extracts posted transactions with **stable IDs**:
   `sha256(account_last4 | bank_date | amount | direction | descriptor)`.
3. Keeps a **durable append-only store** (JSON state file): first_seen is
   never rewritten, rows are never deleted, captures are recorded by artifact
   hash so `--quick` can skip re-ingest.
4. Matches rows against Applied Pay payouts by **literal transfer-reference
   binding only** (the reader's `TRN*1*` candidates). Amount/date alone never
   matches.

## WELLS_ACCESS_MODE

| Value      | Behavior |
|------------|----------|
| `captured` (default) | Ingest captured pages as above. |
| `live`     | **Refuses to run.** Reserved for when Carlo's read-only Wells access lands. |

Until live access exists, **every bank-side claim is labeled `UNVERIFIED`** —
on the report (`verification_label`), on each row, and on each match.
`clears_funds` is always `False`: posted is never converted to cleared.

## CLI

```
python -m applied_pay.wells_proof_job \
  --capture-dir /var/lib/robie-applied-pay-proof/captures \
  --payouts /var/lib/robie-applied-pay-proof/payouts.json \
  --state /var/lib/robie-applied-pay-proof/state.json \
  --out /opt/streetsmart-hermes/applied-pay/reports/proof-YYYYMMDD.json \
  [--quick]
```

`--capture-dir` holds `<name>.capture.json` + `<name>.artifact.json` pairs.
`--quick` skips captures already ingested — the mode the hourly desk sweep calls.

## Scheduling

Daily full reconciliation: `deploy/systemd/robie-applied-pay-proof.{service,timer}`.
Quick mode for the hourly desk sweep: `deploy/systemd/robie-applied-pay-proof-quick.service`.
Install handoff for Dusty: `docs/APPLIED_PAY_PROOF_SCHEDULE_HANDOFF.md`.
Units are shipped but NOT installed by this change.
