# StreetSmart Accountability — Production Activation Handoff

Date: 2026-09-06
Target: `streetsmart-accountability-prod`, project `streetsmart-hermes-poc`, zone `us-east1-b`

## Verified state

- The `ubuntu` crontab runs the accountability launcher at 9:00 AM Eastern, Monday through Friday.
- The reporting date is the previous business day; Monday resolves to Friday.
- The server's persistent EZLynx Chrome service is enabled and active.
- The read-only live Submission Center adapter is present. It preserves the approved agency scope, 100-row page size, Status ordering, first-closed-row boundary, live red-overdue test, day-31 rule, direct links, and count reconciliation.
- RingCentral saved report `Yesterday Calls` uses the dynamic Yesterday range and includes Queues, Users, and Calls.
- One daily RingCentral Excel subscription now delivers `Yesterday Calls` to the Robie reporting mailbox. The older duplicate pointed to the stale fixed-date `Today Calls` report and was removed; recreation is the recovery path if it is ever needed.
- RingCentral mailbox intake accepts the actual subject `Scheduled Reports from RingCentral`, recognizes the `Yesterday Calls` attachment label, and rejects evidence whose call rows or filters do not match the requested business day. This prevents a Monday job from accepting Sunday data merely because that email is newest.
- The existing schedule remains fail-closed: incomplete or unauthenticated inputs prevent the Google Doc update and team-lead delivery.

## Required human handoffs

1. EZLynx is forcing the SSRobie account through a password-reset page. Carlo must complete that reset privately and update the existing Secret Manager password version. Never paste the password into Chat, Git, logs, or a command.
2. Google Workspace domain-wide delegation client `112650695780807418521` still needs these scopes while retaining the approved Gmail scopes:
   - `https://www.googleapis.com/auth/documents`
   - `https://www.googleapis.com/auth/spreadsheets.readonly`
   Live read-only checks from the server returned `unauthorized_client` for both.
3. Magellan's saved server browser state is expired and no Magellan credential secret exists. Establish a fresh authenticated Production browser session through an approved human login handoff; never copy browser cookies or credentials into Git.
4. In EZLynx Reports 5.0, create or correct four weekday CSV/XLSX subscriptions to the Robie reporting mailbox before 9:00 AM: Activities, overdue Tasks, Sales Center, and Policy Changes. Submission Center remains live-browser evidence because no scheduled report exists.

## Release path for the remaining code

Do not patch the live VM directly. Use the accountability feature branch, run the repository checks, build one immutable archive, deploy it to Test, verify the target-date and missing-source failure cases, obtain Carlo's approval for that exact digest, and promote the same digest to `streetsmart-accountability-prod`. Record the rollback pointer and post-promotion evidence.

## Acceptance proof

A complete Production activation requires one dry run for a known prior business day showing all of the following before delivery is enabled:

- RingCentral Queues, Users, and Calls match the requested date and include the explicit hold/wait fields needed for the over-120-second tracker.
- EZLynx Activities, overdue Tasks, Sales Center, and Policy Changes attachments are current and complete.
- The live Submission Center audit and Magellan collection both return authenticated, target-date evidence.
- Google Docs and Sheets delegated reads succeed.
- The persistent pageless department document and department workbook reconcile every collected row.
- Recipient preflight passes, then the approved team-lead email and link-only Chat message are read back successfully.
