# Certificates / Hello intake and daily server report — September 26 candidate

## Requirement

Carlo requested that Certificates and Hello emails and documents reach the correct
existing EZLynx client file without duplicate accounts or notes, with a human review
task and a daily report of server activity. Confirmed mailboxes are
`certificates@streetsmart.insurance` and `hello@streetsmart.insurance`.

## This candidate

`python -m robie_job_engine.server_daily_report --db /path/to/jobs.db --date 2026-09-26`
reads the existing Job Engine database in a read-only consistent snapshot and
prints a plain-text report. Add `--format json` for structured output. Dates are
local calendar days in America/New_York, including daylight-saving transitions;
`--timezone` may explicitly override this. No credentials, source emails, customer
names, note bodies, document contents, or raw exception messages are exported.

The report lists jobs with recorded activity, current statuses, current open work
(including older jobs), failed/uncertain attempts, completion claims without current
authoritative proof, verified jobs whose latest evidence was recorded that day,
recorded existing-task reuse, and recorded manual-upload instructions (current fulfillment unverified).
A job count is not a document/note count. Replayed evidence never multiplies the
verified job count. Historical reports clearly retain current-status semantics.

The intake worker now records whether it reused an existing task or requested
creation. The latter is only a receipt and is never labeled a completed write.
The durable adapter may reconcile an earlier write, so it is deliberately not
called a "new task" count. Existing duplicate-prevention behavior is preserved.

This command does not send, schedule, install, or change Production. It does not
claim mailbox polling ran. Both mailbox coverage fields remain UNVERIFIED until
a separate, authoritative mailbox run/heartbeat contract is implemented. Missing,
unreadable, malformed or incompatible evidence fails instead of becoming zero
activity. A report delivery failure or absent daily report still needs a separate
watchdog; a stopped server cannot reliably report its own outage.

## Verified repository findings, not live-server claims

- `intake_core.py` already requires exact account/policy matching and holds on
  incomplete or ambiguous reads. It has no account-creation operation.
- `ezlynx_intake_task_adapter.py` reserves a durable source key before task writes
  and reconciles uncertain outcomes before any repeat. Live task/ownership ports
  remain unfinished in these Phase 1 components.
- Older intake handoffs say DocumentApi is unavailable. Current `ezlynx_api.py`
  and `ezlynx_api_only_writes.py` now implement DocumentApi upload/search and
  DiscussionApi note read-back. Repository code is not tenant or deployment proof.
- The current intake still sets MANUAL UPLOAD REQUIRED. It is not wired to those
  newer upload/note helpers. Do not remove that obligation until destination
  verification and folder/label/policy association are connected.
- Notes append to existing titled discussions only. Before wiring email intake,
  add durable per-message/per-attachment write reservations and existing-note/
  document checks; an upload helper alone is not a duplicate-safe processor.
- Source keys currently distinguish Gmail mailbox IDs. Copies of the same email
  in both inboxes need a cross-mailbox reconciliation rule, with contradictory
  account matches held, before enabling both listeners.
- Hello routing currently maps renewal to applicable_csr; the supplied current
  SOP specifies the renewal team. Resolve the real team/user mapping before live
  assignment. Do not infer numeric usernames from the walkthrough authors.
- The Certificates SOP still marks discussion title and Automation Center as
  TO CONFIRM. No mapping was invented. Returned Mail is not a generic Hello folder.
- No server inventory or runtime evidence was obtained in this change. Do not
  start a second listener over an existing scheduler.

## Before server activation

1. Inventory existing listeners and their exact release/configuration without
   exposing credentials. Verify read access to both mailboxes on the server;
   desktop connector availability is not server authorization.
2. Connect exact applicant/policy lookup, existing related work, named discussion,
   active assignee/team resolution, task write and independent read-back. Route
   Hello by its document-specific SOP; hold missing classification or ownership.
3. Implement and verify message/attachment deduplication across inboxes, retries,
   restarts, prior manual uploads and partial success. Never retry an uncertain
   note/document write blindly. Preserve original source in private storage.
4. Wire each flow to durable Job Engine jobs. Save source, document/note/task IDs,
   per-stage results and independent evidence. Archive only after required stages
   and routing are verified; an assigned intake task is not completed service.
5. Confirm daily report destination and schedule with Carlo. Delivery needs a
   durable per-report window key, sent-message read-back and no blind resend.
   Schedule only on the server, with a separate missed-run watchdog. Report both
   successes and unresolved work, including partial uploads and unmatched clients.
6. Follow AGENTS.md: Test evidence and independent QA first, then separate Release /
   Production promotion of the exact approved digest. No live deployment is implied.

## Candidate evidence / handoff

Branch: `feat/inbox-audit-report`. PR and commit: recorded in the PR after commit.
Test release/digest: UNVERIFIED. Rollback target: UNVERIFIED; no deployment made.
Local focused tests cover 65 existing intake cases plus 8 report cases. Broader
regression results are recorded in the PR; local synthetic passes are not Test-VM
certification. Independent review requested under the repository QA contract.

QA scenarios: repeated source, existing task reuse, timeout/restart reconciliation,
wrong account, incomplete lookup, complete without evidence, later failed read-back,
later proof outside report day, duplicate evidence rows, older backlog, midnight
and DST boundaries, missing DB, read-only report, and source-content exclusion.

Sources: Certificates SOP `18-rHfPe4pe0pUon_I3Vr9NpVFX3DNij_GvkhgcNslOU`;
Hello SOP `1ypTB9sf1TaSPA0F_hW8ODezvFQpQRln-BywiNwcGW40`;
Steffany guide `15JOD-3Jg-F20tj19E0Q8a_HHXkgxo3xS_kLyfmNLECw`;
Alejandro guide `1fdEsATwZRFQT-i89E2NpOS-VwwEuHCsRjYXQ8jL4u5k`.
