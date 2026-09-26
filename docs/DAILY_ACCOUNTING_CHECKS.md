# Daily Accounting Team checks: Stage 1 draft

This branch is **not deployed or scheduled**. The core and adapters are read-only and
produce a candidate report for the 5:30 AM briefing. No source access or posting
is enabled by merging this branch alone. A production run needs Carlo's separate
go-ahead, confirmed source access, and a review of collector coverage.

## Scope and report contract

- Ascend: cancellation returns, invoice/payment status, programs and finance
  agreements, loans, payouts. Every collection must attest pagination exhausted;
  the existing `ascend_sync.py` methods each take one 50-record page and **must
  not** be used as a complete daily feed.
- Applied Pay: returns and chargebacks from all expanded portal batches for the
  target day, with batch reference, PSP reference, date, and amount. The existing
  validated batch-settlement email reader is useful evidence, but those emails
  alone cannot prove all returns have been captured. Portal view/export
  completeness must be proven before the source is marked complete.
- EZLynx: all open Accounting Team tasks with ID, status, due date, applicant,
  observed time and title. The current productivity CSV parser aggregates by
  person and drops task identity/due dates. It cannot feed this item-level view.
- Bank feeds: reserved for source-grounded receipt/clearing checks when
  connected. QBO can corroborate but cannot replace bank proof. No feed is
  connected by this branch. Until then clearing is explicitly unproven.

`daily_accounting_inputs` accepts externally collected JSON snapshots; a
complete claim requires an as-of time and explicit full-scope/pagination
attestation. It does not authenticate the attestation, and must not ingest an
untrusted note as approval or bank evidence. If any source is missing, partial,
malformed, stale or not actually paginated to exhaustion, do not label that
source "no events". The first verified snapshot is a baseline. Subsequent
item changes carry both prior and current field-level evidence. A fresh poll
of unchanged records must not appear as a change. Open Accounting tasks are
listed even when unchanged.

The scorecard records every claimed item and an *independent* re-check,
including source ID and check time. Unchecked items are `unknown` and are
included in the accuracy denominator. Do not mark an item correct from its
own source assertion. Preserve prior scorecards as append-only run artifacts
in the integration stage, with stable run IDs and independent reviewer source
references; this branch computes the scorecard but does not write an artifact.

## Integration gate before running

1. Implement and verify collectors against the live Ascend pagination schema,
   complete Applied Pay portal date boundary, and EZLynx task endpoint/export.
   Add tests for page loops, row caps, late changes and failed authentication.
2. Wire fresh bank feed and optional QBO evidence, with transaction identity,
   bank account mapping, posting date and amount checked against each item;
   never interpret a finance-platform status alone as a cleared deposit.
3. Wire immutable report storage and the 5:30 AM briefing reader with explicit
   source freshness limits, duplicate-run protection, and redacted evidence.
4. Independently re-check candidate findings and review the scorecard. Do not
   activate, post to QBO/EZLynx, send alerts, or change production on this PR.

Synthetic tests contain no client data. Run:
`PYTHONPATH=. python -m pytest -q tests/test_daily_accounting_checks.py tests/test_daily_accounting_inputs.py`

The offline prototype CLI can render a candidate, first-run baseline:
`PYTHONPATH=. python -m robie_job_engine.daily_accounting_cli [--ascend FILE] [--applied-pay FILE] [--ezlynx FILE]`.
It makes no network calls or source-system writes. It does **not** certify the
provided JSON exports; source collectors, previous snapshot persistence, and
briefing publication are still unbuilt. Missing files explicitly show incomplete.
