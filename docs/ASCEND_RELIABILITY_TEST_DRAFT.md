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

## Job 1, October 4: live destination ports (stacked on #764 / #759)

Labels: **BUILT** = code and tests ran, with output below. **DESIGNED** = code is
written but has not run against a live system yet; the command to run it is
listed and has not been run.

### BUILT (local, Python 3.11, fakes for every network call)

- `ascend_destinations.EZLynxAscendDestination`: note and task components on the
  applicant's `Tasks by Robie` discussion (created with the first write if missing)
  through the #764 direct Task API login and gates (write allowlist + driver lease,
  no phone numbers). Every write carries `[ascend:<key>:<component>]`.
  `find_for_event` reads every `Tasks by Robie` discussion of the applicant. That
  is the only place this port writes, so a clean read is an authoritative absence.
  Readback finds each id inside those discussions, so the applicant binding
  comes from EZLynx, not from this process. A task must read back as a
  `TaskCreationNote`.
- `ascend_destinations.QBODepositDestination`: one Deposit per commission payout
  with the mapped bank account, income account, payee, amount, and currency.
  `find_for_event` scans every Deposit page in a 45-day window for the marker.
  Readback checks realm, account, income account, payee, amount (cents), currency,
  and the marker. A Production QBO company is refused before any request unless
  `ROBIE_ASCEND_QBO_PRODUCTION_WRITES=1`, which this change never sets.
- `ascend_destination_mapping`:
  - Applicant: exact policy-number match only. Every matching PolicyApi row
    must name the same applicant. No insured-name match.
  - Task assignee: the CSR login from the Ascend program producer via the
    notice driver's `resolve_cancellation_csr` (accounting issues use
    `ROBIE_ASCEND_ACCOUNTING_ASSIGNEE`). PolicyApi rows carry no CSR field.
    It needs a confirmed numeric EZLynx user id.
  - Payout: realm from the QBO config, plus the account and payee ids named by
    `ROBIE_QBO_ASCEND_DEPOSIT_ACCOUNT_ID`, `ROBIE_QBO_ASCEND_COMMISSION_INCOME_ACCOUNT_ID`
    and `ROBIE_QBO_ASCEND_PAYEE` (`Vendor:<id>` or `Customer:<id>`; see the October 4 revision).
  - `verify_qbo_mapping` reads those ids back from QBO (read-only).
- `ReliableDelivery`:
  - Ports may answer `find_for_event(event)`.
  - `PAYOUT_BINDING` now includes `income_account_id`.
  - A port that proves nothing was sent (`not_sent`: allowlist refusal,
    unconfirmed assignee, QBO Production refused) clears that component's
    attempt, so the next run retries. This is #759's "leave pending" contract
    on the new path.
  - A send that may have landed stays lookup-only, as in #747.
- `paginate` truncation fix:
  - The bug: Ascend `/users` (proven live) pages with `meta.next` = page
    number. The old helper treated a page with `meta: {"next": 2}` as the last
    page and silently dropped every later page.
  - Now `meta.next` is followed as `?page=N`, a null `meta.next` ends the walk,
    and the cursor contract is unchanged.
  - A full page with no recognized continuation field, a repeated page, or a
    record id seen on two pages raises.
- `AscendEZLynxSyncManager.deliver_test_once`:
  - Explicit and Test-only: it refuses outside `ROBIE_ENV=TEST`. `sync_once`
    and the daemon stay disabled.
  - Only events mapped to an allowed applicant (Buster Brown `26356199` by
    default) reach a live port. Everything else is staged.
  - COMPLETE is reported only for `delivered_readback`.
- #747/#759 conflict resolution:
  - `ezlynx_note_poster.create_task` keeps #759's honest result.
  - #747's source-text test is replaced by a behavioural one (a missing client
    returns `error`, never `success`).
  - #759's sync honesty tests now check that disabled `sync_once` refuses and
    marks nothing done.

Local test output (branch `feat/ascend-reliability-live-ports`):

```text
tests/test_ascend_destinations.py tests/test_ascend_delivery_state.py
tests/test_ascend_sync.py tests/test_ezlynx_task_api.py           90 passed
tests/test_ezlynx_create_task_honesty.py                           6 passed
full tests/: 152 failed, 5658 passed, 16 skipped
  the 152 failures are the same tests that fail on main and on #759 on this
  Mac (VM, release and operator tests that need the Linux host); none is new.
merge commit before this work (#764 + #747): 156 failed, 5635 passed
  (the 4 extra were the #747/#759 conflicts fixed above)
```

### DESIGNED (written, not run against a live system)

| Item | Script | Live effect |
|---|---|---|
| Full regression on hermes-test-01 | commands below | none (hermetic env) |
| Ascend pagination proof (sandbox) | `scripts/ascend_pagination_probe.py` | read-only GETs |
| QBO mapping check | `scripts/ascend_qbo_mapping_check.py` | read-only GETs |
| Buster Brown destination proof | `scripts/ascend_live_destination_proof.py --ezlynx` | **writes**: 1 note + 1 task on Buster Brown. HELD for Dusty and Moe |
| QBO sandbox deposit proof | `scripts/ascend_live_destination_proof.py --qbo-sandbox` | **writes** one $1.00 sandbox Deposit. HELD; needs sandbox credentials |
| Real-event Test delivery | `scripts/ascend_delivery_test_run.py --live` | **writes** for Buster-mapped events only. HELD |

Not built:
- Multiworker leases.
- Supplier accounting (still disabled).
- Production QBO writes.
- Reconciling legacy-ledger rows against destinations. The read-only planner
  from #747 is unchanged.
- An Ascend event whose policy maps to Buster Brown. Sandbox events won't match
  his live EZLynx policy, so the live proof uses one clearly labelled
  synthetic event instead.

### Test-box commands (hermes-test-01), in order

Step 1 (read-only, safe now). This makes a scratch copy and runs the full
regression. The environment is hermetic: no `ROBIE_*` variables and a
scratch `HOME`.

```bash
set -euo pipefail
W=$(mktemp -d /tmp/ascend-job1-XXXXXX)
git clone --quiet --branch feat/ascend-reliability-live-ports https://github.com/streetsmart-insurance/robie-hermes.git "$W/repo"
cd "$W/repo" && git rev-parse HEAD
python3 -m venv "$W/venv" && "$W/venv/bin/pip" -q install "pytest>=8.3,<9" "openpyxl>=3.1,<4" "pypdf>=5,<7" "PyYAML>=6.0,<7"
mkdir -p "$W/home"
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$W/home" PYTHONPATH=.:tests "$W/venv/bin/python" -m pytest -q -p no:cacheprovider -rfE tests 2>&1 | tail -60
echo "scratch=$W"
```

Step 2 (read-only, live Ascend sandbox and QBO, safe now). This uses the Test
release interpreter, which has the Secret Manager client. No secret is printed.

```bash
cd "$W/repo"
ROBIE_ENV=TEST PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_pagination_probe.py
PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_qbo_mapping_check.py
```

Step 3 (dry run, no HTTP):

```bash
PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_live_destination_proof.py
```

Step 4: **HELD until Dusty and Moe are there. Writes to Buster Brown.**

```bash
ROBIE_ENV=TEST ROBIE_EZLYNX_DISCUSSION_API=live ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199 \
ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME=<EZLynx agency login> \
ROBIE_EZLYNX_TASK_API_STATE_DIR="$W/task-api-state" \
PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_live_destination_proof.py --ezlynx --ledger "$W/proof.db"
```

The proof passes only with `PROOF PASSED`. That means:
- the first pass ends `delivered_readback`, with the note and task ids read
  back from Buster Brown's `Tasks by Robie` discussion;
- the second pass, on a fresh ledger, also ends `delivered_readback` and
  sends nothing.

Cleanup: `rm -rf "$W"`. Nothing under `/opt`, no runtime pointer, no service
and no live job is touched by steps 1-3.


## Revision after Test-box review, October 4

**(1) Paging past page 1.** The sandbox run passed, but every list fit on one
page, so page 2 was never exercised.

BUILT:
- `tests/test_ascend_pagination_multipage.py` drives the real
  `AscendApiClient.get` HTTP path, with only `urlopen` mocked, against a fake
  vendor that serves the proven page-number contract. 6 tests:
  - `fetch_payouts` reads all 3 pages of 125 rows and requests pages 1, 2, 3;
  - cancellation returns, programs and payouts each cross 3 pages;
  - a vendor that ignores `page` raises `duplicate_record_across_pages`
    instead of returning a short list;
  - the cursor contract also crosses pages;
  - the probe reports `multi_page_proven` with a small page size;
  - the probe says `MULTI-PAGE NOT PROVEN` when everything fits on one page.
- The probe takes `--page-size` and `--endpoint`, also checks `/v1/users`, and
  prints `MULTI-PAGE PROVEN on: ...` or `MULTI-PAGE NOT PROVEN`. It reports
  `multi_page_proven` per endpoint only when the walk really crossed a page and
  `paginate` returned the same ids.

DESIGNED (how to get a real multi-page sample, read-only, sandbox):
- `--page-size 1` makes any list with two or more rows span several pages:

  ```bash
  ROBIE_ENV=TEST PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_pagination_probe.py --page-size 1
  ```

  Read `page_shapes[].rows` per endpoint. All 1s mean Ascend honors
  `page_size`, and page 2 onward was walked. If a page holds more rows than
  asked, Ascend ignores `page_size` for that list, and a real sample then
  needs more rows than its fixed page size.
- `/v1/users` is the list proven multi-page in Production (25 per page, page 2
  of 39). In the sandbox it pages only if the sandbox org has more than 25
  users, or more than the page size you pass.
- If every sandbox list has at most one row, nothing read-only can prove
  page 2. Two options, both for you to decide:
  - create a few sandbox programs in the Ascend sandbox dashboard (a sandbox
    write);
  - run the same probe read-only against Production `/v1/users`, which is
    known to have 39 pages. That is a Production read, outside this Test-only
    job.

**(2) QBO payee entity type.** Vendor and Customer ids overlap in QBO. The
verifier used to try Vendor and then Customer, so a bare id could resolve to
the wrong record.

BUILT:
- The setting is now `ROBIE_QBO_ASCEND_PAYEE=Vendor:<id>` or `Customer:<id>`.
  A bare id, an unknown type or a non-numeric id is refused
  (`payout_payee_setting_invalid`). `ROBIE_QBO_ASCEND_PAYEE_ID` is gone.
- The verifier reads only the configured type and never falls back to the
  other.
- The deposit line sends `Entity {value, type}`. `payee_type` is part of the
  exact payout binding, and readback takes the type from the deposit line.
  If QBO omits it, readback reads the payee as the configured type and accepts
  it only when that record's display name equals the name on the line.
- 7 new tests:
  - Vendor 12 and Customer 12 both exist, and only the configured one is read;
  - an inactive Vendor 12 never falls through to Customer 12;
  - a bare id is refused;
  - a deposit recorded against the other type is `destination_unverified`;
  - a line with no type passes by matching name and fails on a mismatched name.
