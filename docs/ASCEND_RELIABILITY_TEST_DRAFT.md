# Ascend reliability candidate - Test only

Status: BUILT candidate, NOT QA-certified, NOT deployed to the live runner.

## Boundary
No Production changes, merges, legacy ledger edits, QBO/EZLynx writes or
credential changes. Test execution was isolated under /tmp. No runtime pointer
was changed. Supplier accounting is disabled, not implemented.

## Built
- Separate source_seen / matched / staged / attempted / delivered states.
- Retry unmatched mappings; row existence is not a delivery receipt.
- Separate durable SQLite candidate storage with a pre-send commit. Single worker.
- Require destination IDs and source-key/applicant readback through injected ports.
- Never resend an uncertain attempt; lookup/readback only after restart.
- Existing sync_once is read/stage only. Legacy orchestration raises immediately.
- Remove supplier success prose and EZLynx task success-shaped fallback.
- Cursor pagination helper rejects loops, arbitrary next URLs and ambiguous full
  pages. Existing list methods use it, but vendor cursor binding is UNVERIFIED.
- Synthetic replay of the 53 observed shapes: 25 cancellations, 3 signed,
  7 flagged supplier issues, 2 staged paid payouts, 16 unsupported branches.

## DESIGNED / not built or proven
- Live QBO/EZLynx destination adapters and source-marker recovery search.
- Exact business-content readback (amount, account, payee, attachment/body) and
  split note/task recovery. ID/source/applicant checks alone are insufficient.
- Vendor pagination contract and end-to-end feed completeness.
- Legacy ledger migration/reconciliation, human-created destination exclusion.
- Multiworker leases and durable candidate storage wiring into scheduled runner.
- Real mapping improvements and actual destination outcomes.

Do not enable live destinations or promote this candidate. Next QA must cover
partial delivery, restart, wrong mapping, wrong amount/payee/account, existing
human records, every page shape, and staging with zero writes. A separate scoped
authorization is required before any live destination effect.

## Test proof
The replay uses synthetic source IDs/counts, not copied customer payloads. Package
__init__ was isolated only in the temporary Test harness, not published in the PR.
22 regression tests passed on Test. This is library/static-safety proof, not
installed-runner or vendor-delivery proof.

```text
hermes-test-01.c.streetsmart-hermes-poc.internal
----------------------------------------------------------------------
Ran 22 tests in 0.098s

OK
{
  "source_shapes": 53,
  "first_pass": {
    "unmatched_retryable": 28,
    "staged_no_live_destination": 8,
    "supplier_accounting_disabled": 1,
    "unsupported_type_status": 16
  },
  "mapping_retry_pass": {
    "staged_no_live_destination": 36,
    "supplier_accounting_disabled": 1,
    "unsupported_type_status": 16
  },
  "destination_writes": 0,
  "delivered": 0,
  "legacy_ledger_edits": 0,
  "network_calls": 0,
  "data": "synthetic counts and shapes only"
}
ba1543ee8b296ff4186d5cc8a58f2e104fd7f263adca2eaea4c091862dd714cd  robie_job_engine/ascend_delivery_state.py
isolated_directory=/tmp/ascend-reliability-QEnUsI
/opt/streetsmart-hermes/current
```

Reproduce from repo using an isolated namespace harness, so package initialization
cannot load application configuration or credentials. No secret/network adapter
is included in the replay.

Rollback: no installed version or runtime pointer changed. Remove the isolated
/tmp candidate only if desired; do not touch legacy state. Draft branch is the
only repository change and must remain unmerged pending independent QA.
