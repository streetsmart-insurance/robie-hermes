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
