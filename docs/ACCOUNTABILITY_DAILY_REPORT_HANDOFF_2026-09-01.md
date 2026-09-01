# StreetSmart Daily Accountability — Cloud Handoff

Date: 2026-09-01  
Environment: Test only  
Production: Not touched or authorized

## Current operating state

- Automation ID: `previous-business-day-accountability`
- Status: Active
- Schedule: Monday–Friday at 9:00 AM America/New_York
- Report period: previous business day; Monday reports Friday
- Temporary recipients: Carlo, Jake, and Ashley
- Google Chat: credential configured outside Git; delivery paused until Carlo approves the exact recurring link-only message
- Daily Excel deliverable: required, uploaded as a downloadable `.xlsx`, shared with all current recipients, and linked prominently in the email; attach it when the mail action supports attachments

## Verified manual delivery

The August 31, 2026 package was sent from the approved StreetSmart reporting account to Carlo, Jake, and Ashley on 2026-09-01. The immutable Gmail receipt and access-controlled artifact URLs are retained in the private operator evidence, not in this public repository.

- Subject: `StreetSmart Daily Accountability — August 31, 2026 — Complete Excel Workbook`
- Private package: downloadable Excel workbook, pageless Google Doc, all-data Google Sheet, visual dashboard, reporting runbook, and Antigravity-ready skill

## Gabi verification and re-enablement

The live AppSheet backing sheet now shows Gabi as Active, with the expected StreetSmart work mailbox, Trucking and Transportation Department Manager position, and Admin role. The earlier inactive-roster finding is stale.

Do not add Gabi back to the daily distribution until Test proves delegated Gmail access for the exact mailbox:

1. On `hermes-test-01`, use only the Test accountability service account and the approved `gmail.metadata` scope.
2. Impersonate Gabi's approved work mailbox and call a metadata-only Gmail endpoint such as profile read or a bounded message-list request.
3. Confirm the returned profile address exactly equals the approved roster address.
4. Run the same fail-closed mailbox preflight for every daily recipient in one cycle.
5. Store only pass/fail evidence and the verified address; do not read or retain employee email bodies.
6. After the Test proof passes, update the automation recipient list and run one manual Test delivery before relying on the next schedule.

Roster Active status does not prove domain-wide delegation. A 401/403, subject mismatch, missing mailbox, or ambiguous address remains a failed preflight.

## RingCentral daily contract

Every run must export the complete RingCentral Detailed Call Log and queue analytics for the exact previous business day from 9:00 AM through 5:00 PM America/New_York. Never reuse a prior export.

Required reconciliation:

- Personal Lines, Commercial, Trucking, and lead queues
- parent calls separated from routing legs
- answered, missed, abandoned, and voicemail outcomes
- later connected outbound calls to the same number
- later inbound redials answered by a human
- AI-only answers do not close callback cases
- zero pickups do not prove that an employee missed an offered queue call
- client callback candidates remain visible even when Carlo is excluded from employee phone scoring

Missing or incomplete RingCentral export evidence makes the report `UNVERIFIED` and blocks the scheduled send.

## Delivery gates

Before email delivery, verify the current recipients against the live active roster and the authenticated Test Gmail profile. The send fails closed if any required report, Excel workbook, Drive share, recipient verification, or source export is missing.

The email must include:

- the complete pageless Google Doc
- the downloadable Excel workbook and all-data Google Sheet
- the visual dashboard
- the reporting runbook
- the packaged skill
- a request for Jake and Ashley to scrutinize the findings and return corrections

Employee Gmail review is metadata-only. Full client details remain in access-controlled internal artifacts. Production remains disabled.

## Rollback

Pause `previous-business-day-accountability`, retain the evidence package and Gmail receipt, and revert only the automation recipient/delivery change. No Production rollback is required.
