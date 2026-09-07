# StreetSmart Accountability — Production Activation Handoff

Date: 2026-09-06
Target: `streetsmart-accountability-prod`, project `streetsmart-hermes-poc`, zone `us-east1-b`

## Verified state

- The current Production `ubuntu` crontab runs the accountability launcher at 9:00 AM Eastern, Monday through Friday. This candidate changes the verified recurring schedule to 6:25 AM Eastern; Production remains unchanged until immutable promotion.
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
3. Magellan's normal Carlos account login was verified, its password was rotated, and `magellan-username` plus `magellan-password` now exist in Secret Manager. The candidate branch adds a separate port-9223 persistent browser and a read-only prior-business-day collector. It still requires Test verification and exact-digest Production promotion; do not copy cookies or credentials into Git.
4. In EZLynx Reports 5.0, create or correct four weekday CSV/XLSX subscriptions to the Robie reporting mailbox early enough to arrive before 6:25 AM: Activities, overdue Tasks, Sales Center, and Policy Changes. Submission Center remains live-browser evidence because no scheduled report exists.

## Release path for the remaining code

Do not patch the live VM directly. Use the accountability feature branch, run the repository checks, build one immutable archive, deploy it to Test, verify the target-date and missing-source failure cases, obtain Carlo's approval for that exact digest, and promote the same digest to `streetsmart-accountability-prod`. After promotion, reconcile the live schedule as `25 6 * * 1-5` in `America/New_York`, confirm the first run targets the prior business day, and record the rollback pointer and post-promotion evidence.

## Magellan credential and candidate evidence

- The ordinary Magellan login was independently verified in a fresh isolated browser session as Carlos on 2026-09-06.
- Secret Manager now contains `magellan-username` and `magellan-password`. Password version 2 is enabled and the temporary version 1 is disabled. Secret payloads are not stored in this repository or handoff.
- Candidate files add a dedicated persistent Magellan Chrome profile on localhost CDP port 9223, leaving EZLynx on port 9222 untouched.
- The collector reads call date/time, From, To, duration, sentiment icon, tags, and handled state; it never opens call details or collects transcripts.
- The expanded accountability and RingCentral suite passed 82 tests locally. The skill validator and `git diff --check` passed. A repository-wide simulator invocation produced no result before it was stopped after several minutes; it is not counted as a pass and CI remains required. The earlier full regression battery also had two macOS-local rollback failures because GNU `install -D` is unavailable; neither open item is a Production waiver.
- Production status remains **UNVERIFIED / not promoted** until the feature branch is reviewed, one immutable candidate is deployed to Test, the live Magellan dashboard is collected for a known prior business day, the generated department report reconciles to the dashboard, and Carlo approves the exact candidate digest.

## Target-host distinction

- `streetsmart-accountability-prod` is the dedicated accountability VM. It currently runs the standalone `/opt/streetsmart-daily-accountability` application from the `ubuntu` crontab at 9:00 AM Eastern.
- `hermes-poc-01` is the general Hermes Production VM targeted by the existing repository Production installer. They are different instances. Never use the `hermes-poc-01` installer as proof that the dedicated accountability VM changed.
- The dedicated VM runner currently contains a one-date Labor Day skip for 2026-09-07. That live file and its 9:00 AM crontab remain unchanged until a reviewed, Test-verified, dedicated accountability release path is promoted. Therefore the requested 6:25 AM September 7 run is not yet installed.

## Acceptance proof

A complete Production activation requires one dry run for a known prior business day showing all of the following before delivery is enabled:

- RingCentral Queues, Users, and Calls match the requested date and include the explicit hold/wait fields needed for the over-120-second tracker.
- EZLynx Activities, overdue Tasks, Sales Center, and Policy Changes attachments are current and complete.
- The live Submission Center audit and Magellan collection both return authenticated, target-date evidence.
- Google Docs and Sheets delegated reads succeed.
- The persistent pageless department document and department workbook reconcile every collected row.
- Recipient preflight passes, then the approved team-lead email and link-only Chat message are read back successfully.
