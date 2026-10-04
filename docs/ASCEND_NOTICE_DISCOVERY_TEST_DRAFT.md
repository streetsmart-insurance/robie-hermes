# Ascend email discovery - step 1, Test-only draft

Built separate discovery module. Existing live driver, services, mailbox scope,
Production configuration and sync PR #747 are unchanged.

## Built
- Explicit mailbox and bounded start/end dates; no unread-only or two-day cap.
- Search leads: Ascend sender domain, useascend.com text or Ascend brand text.
  Dates use Gmail after/before semantics. Boundary-day completeness is not proven.
- Gmail nextPageToken traversal, duplicate-ID guard, loop/page-budget refusal.
- Plain-text and HTML forwards, original-sender claims and Ascend links.
- Payment-failed, past-due, cancellation, refund/return-premium and new-program
  candidate families. Ambiguous/unknown Ascend-looking messages stay review.
- Separate private SQLite review queue, mailbox/message idempotency, mode 0600.
  Stores subject, sender claim, body hash/excerpt and explicit no-action flags.
- Per-message failures retained as review items; incomplete scan never returns
  complete. Listing failure, page cap and loop also report incomplete.
- Test-only CLI refuses non-Test environment before client creation; constructs
  delegated Gmail service with modify=False. No destination adapter exists.

Sender claims, quoted From lines, brand text and URLs are not authentication.
Every notice remains source_verified=False and may_file/may_send_task=False.
No email content instruction is interpreted or executed. Attachments and remote
links are not downloaded. Attachment-only notices need later manual review.
Cross-mailbox copies remain distinct; no unsafe body-only cross-mailbox dedupe.

## Test proof
Real package imports from Test release 7e54ed7f1895 in a copied /tmp directory.
27 tests passed in 0.21s. Synthetic replay: two pages, 53 review candidates,
35 already-read messages included. Repeat scan leaves 53 persisted rows, not 106.
Queue mode 0600. Only Gmail list/get test-double methods called. No live Gmail
scan, label modification, destination write or legacy ledger change.
Module SHA256 b14a20ec0fffcc2e0db7569b2a2a017fdd01d0d4b0a74d714788ae04bcbe2c03.
No installed release/pointer change; Test remained 7e54ed7f1895.

Reproduce: ROBIE_ENV=TEST PYTHONPATH=.:tests python -m pytest -q
 tests/test_ascend_notice_discovery.py
Synthetic replay: PYTHONPATH=.:tests python tests/ascend_notice_discovery_replay.py

## Not built / not proven
Actual older/read mailbox backfill, source-authentication checks, search recall
against real notices, attached .eml/PDF notice extraction, complete date-boundary
coverage, review UI/ownership, automated incremental schedule, matching to
Ascend/EZLynx accounts or CSR, task/note writes and authoritative readback.
Local queue is not a live team review list. No backlog-clearance claim.

Before real backfill: choose explicit mailbox/date window and private queue
retention/audience. Do not widen live scope or replace the active service from
this draft. Exact release QA and independent review remain required before
any merge or live work. Step 2 account matching is outside this build.

## Job 2, October 4: always-on runner and live mailbox validation

Labels: **BUILT** = ran, with test output. **DESIGNED** = written, and has
not run against a live mailbox. Propose-only throughout: no note, task,
label or read-state change.

### BUILT (local, fakes for Gmail)

- `robie_job_engine/ascend_notice_review_runner.py`:
  - **Test only:** refuses unless `ROBIE_ENV=TEST` and
    `ROBIE_ASCEND_NOTICE_REVIEW=1`.
  - **Incremental:** each mailbox has a durable checkpoint (the end of its
    last complete scan). A run scans from the checkpoint minus 2 days through
    today. The 2-day overlap re-sees late-indexed mail, and the queue's
    per-mailbox/message idempotency stops it creating duplicates. The checkpoint
    moves only after a complete scan.
  - **No implicit backfill:** the first run starts at
    `ROBIE_ASCEND_NOTICE_REVIEW_START`.
  - **Single instance:** a non-blocking `flock`; a second run reports `skipped`.
  - **Read-only by construction:** the Gmail client is built with `modify=False`
    (readonly scope) and wrapped in `ReadOnlyGmail`, which exposes only
    `messages.list` and `messages.get` (full, metadata or minimal). Any other call
    raises before a request exists.
  - **Propose-only:** every queued item carries `proposal.propose_only=True`,
    `may_file=False`, `may_send_task=False` and `source_verified=False`.
  - **Private state:** queue, checkpoints and `status.json` are mode 0600 under
    `ROBIE_ASCEND_NOTICE_REVIEW_STATE_DIR`.
  - **Per-mailbox isolation:** one mailbox failing (for example, missing
    delegation) marks the run incomplete and does not stop the others.
- `deploy/systemd/robie-ascend-notice-review-test.{service,timer}`:
  - A oneshot run every 15 minutes, limited to `hermes-test-01` by
    `ConditionHost`, with `ROBIE_ENV=TEST`.
  - Sandboxed: `ProtectSystem=strict`, and the only writable path is the state
    directory.
  - Plus `robie-ascend-notice-review.env.example`, which holds names only.
  - **Not installed or enabled** by this change.
- `scripts/ascend_notice_mailbox_validate.py`:
  - A live read-only check of one mailbox over the last N days, into an
    in-memory queue.
  - Prints counts and notice families only (no subjects, senders or bodies).
  - Passes only when the scan is complete with zero destination writes and zero
    label changes.

Local test output (branch `feat/ascend-notice-review-runner`):

```text
tests/test_ascend_notice_review_runner.py + tests/test_ascend_notice_discovery.py   39 passed
full tests/: 152 failed, 5595 passed, 16 skipped
#751 head:    152 failed, 5583 passed, 16 skipped
no test fails on this branch that passes on #751; the 152 are the same
Linux-host tests that fail on main on this Mac
```

### DESIGNED (written; not run, because the mailbox is blocked)

The Gmail delegation service account still does not exist (#760 disabled the
notice driver for that reason). The live validation and the always-on runner
cannot read a mailbox until it does. When it exists, run on hermes-test-01:

```bash
set -euo pipefail
W=$(mktemp -d /tmp/ascend-job2-XXXXXX)
git clone --quiet --branch feat/ascend-notice-review-runner https://github.com/streetsmart-insurance/robie-hermes.git "$W/repo"
cd "$W/repo" && git rev-parse HEAD
ROBIE_ENV=TEST PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python scripts/ascend_notice_mailbox_validate.py \
  --mailbox robie@streetsmart.insurance --delegation-service-account <delegation service account email> --days 7
```

Expect `MAILBOX VALIDATION PASSED`, `destination_writes: 0` and
`gmail_label_changes: 0`. Installing the timer is a separate, later step after
review. This change installs nothing.
