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
- Existing sync_once, daemon and CLI refuse scheduled stage-only operation. Explicit preview_once is read/stage only. Legacy orchestration raises immediately.
- Remove supplier success prose and EZLynx task success-shaped fallback.
- Cursor pagination helper rejects loops, arbitrary next URLs and ambiguous full
  pages. Existing list methods use it, but vendor cursor binding is UNVERIFIED.
- Synthetic replay of the 53 observed shapes: 25 cancellations, 3 signed,
  7 flagged supplier issues, 2 staged paid payouts, 16 unsupported branches.

## DESIGNED / not built or proven
- Live QBO/EZLynx destination adapters and source-marker recovery search.
- Exact business-content readback (amount, account, payee, attachment/body) and
  live split note/task recovery. The injected candidate now supports component recovery, but live ports are absent.
- Vendor pagination contract and end-to-end feed completeness.
- Legacy migration, actual destination reconciliation and human-created record exclusion. Read-only reconciliation planner is now built.
- Multiworker leases and durable candidate storage wiring into scheduled runner.
- Real mapping improvements and actual destination outcomes.

Do not enable live destinations or promote this candidate. Next QA must cover
partial delivery, restart, wrong mapping, wrong amount/payee/account, existing
human records, every page shape, and staging with zero writes. A separate scoped
authorization is required before any live destination effect.

## Test proof
The replay uses synthetic source IDs/counts, not copied customer payloads. Package
__init__ was isolated only in the temporary Test harness, not published in the PR.
Original 22 isolated regression tests passed on Test. Revised real-package suite has 46 passing Ascend tests. This is library/static-safety proof, not
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

## Revision after independent review, October 3

BUILT: six legacy posting tests replaced with explicit refusal/no-write contracts;
preview no-write test; stage-only CLI exits 2 before store initialization; daemon
raises before store initialization. Library calls must use explicit preview_once.

BUILT: component-level injected recovery. A verified existing note is retained.
Only a source/destination-bound authoritative absence result permits sending the
missing task. Each component is durably marked attempted before send. A lost
receipt stays lookup-only even if a later lookup claims absence. Recovery with
both IDs and readback completes without resend. No live component port exists.

BUILT: synthetic=True is no longer a storage bypass. All destinations require
DurableLedger, including test doubles. Payouts require non-null realm/account/payee/
amount/currency exact readback, not None==None on applicant_id. Accounting tasks
require a non-null applicant binding.

BUILT: ascend_legacy_reconciliation.read_only_plan opens the explicitly supplied
legacy database using mode=ro and query_only. It classifies real schema raw_data_json
staged SUCCESS, unmatched and uncertain rows into lookup-only plans. Tests check
source bytes unchanged and the actual AscendSyncStore schema. It does not import
rows, authorize sends, update legacy records, or prove live destination state.

The vendor pagination assumption remains UNVERIFIED. Stage-only preview is not
operational delivery. Live content/accounting and human-record reconciliation
remain unfinished; do not promote.

Revised focused Test run: 46 passed in 1.25s. CLI output:
ASCEND_SYNC_DISABLED: stage-only candidate is not an operational sync
cli_exit=2

Revised module SHA256:
844628bf96be052d4cf69844558e60c8469c4108d03d2c7ec30a473fae8e4c89 delivery_state
 ae524c9dd5408cd8e59c81c1d505717e8c251874bf624afeec5e45219f66deb9 sync
06c92880d7609c89596abbad52821a514fc00b644d02d1014f182078556eaff9 reconciliation

Full-suite result and provenance are reported separately, not implied by the
focused pass. Original PR test assertions were confirmed failing before replacement.
