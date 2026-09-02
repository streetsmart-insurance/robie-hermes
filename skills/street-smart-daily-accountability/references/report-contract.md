# Daily report contract

## Schedule

- Weekdays at 9:00 AM America/New_York.
- Activity date is the previous business day; Monday uses Friday.
- Manual/Test collection remains the default until the cloud release is approved.

## Sources

- RingCentral calls and queue analytics for Personal Lines, Commercial, Trucking, and lead queues.
- Magellan sentiment and call detail.
- EZLynx Reports 5.0 Activity Detail, Change Request Detail, Sales Center Detail, Submission Center, and relevant account Activity discussions.
- Existing policy-change and COI Google Sheets trackers.
- AppSheet backing sheet `Employees`, limited to Name, Position, Email, Department, App Roles, and Employment Status.
- Gmail metadata for approved active employees; Gmail readonly only for the reporting mailbox and attachments.

## Package

1. Pageless native Google Doc with self-contained team-lead tabs. Each department tab includes all applicable calls, queue metrics, employee connected/talk-time totals, calls with explicit hold or queue-wait time above 120 seconds, callbacks, overdue tasks, policy changes, COIs, Sales Center candidates, Submission Center exceptions, Magellan findings, and validation guidance. Separate agency-wide evidence tabs retain complete normalized findings, complete policy-change and COI tracker rows, source links, and approved high-level dashboard graphics.
2. Unique overdue-task CSV with task created, due, last modified, overdue age, account, owner, roster department, EZLynx workstream, and source row.
3. Sales Center CSV with opportunity created date, age, assigned producer, lead source, stage, last-touch evidence, and roster department.
4. Visual dashboard presentation showing agency KPIs, department backlog, queue answer rates, Personal Lines/Jazmin drill-down, policy-change and COI aging, Sales Center ownership and sources, controls, and release decisions.
5. Comprehensive Excel workbook with separate tabs for the summary, department offender ranking, every unique overdue task, full Activity Detail export, full overdue-history export, Sales no-touch exceptions, full Sales Center export, full RingCentral call/routing export, callback candidates with EZLynx account hyperlinks, employee phone totals including direct/queue/outbound connected time, queue metrics and membership scoreboard, explicit hold/wait-over-120-second exceptions, complete policy-change and COI tracker rows, overdue submissions, Magellan sad/at-risk calls, approved employee roster fields, and a validation log.
6. Cloud handoff when the run changes automation, filters, recipients, or release behavior.
7. Google Drive runbook and packaged skill describing collection, reconciliation, validation, document construction, sharing, and Test-only email delivery.

## Concise report section order

Use the same order in the agency summary and every department tab:

1. Phone & Queue Service — over-two-minute hold/wait exceptions first, then queue scorecard, employee call/talk measures, voicemail, and callback service risk.
2. Client Follow-Up — callback verification, repeated callers, EZLynx account links, and email-response aging.
3. Policy Service — policy changes, ownership, dependency, request age, and next action.
4. Sales — Sales Center assigned producer, lead source, stage, last touch, and no-touch age.
5. Tasks & Activities — overdue tasks, applicant/account, owner, age, latest evidence, and department offenders.
6. COIs & Submissions — pending COIs and Submission Center exceptions with original request date and blocker.
7. Customer Sentiment — Magellan sad/at-risk calls and corroborated service concerns.
8. Validation — source window, evidence gaps, unresolved ownership, and release gates.

## Default recipient allowlist

- carlo@streetsmart.insurance
- jake@streetsmart.insurance
- sandy@streetsmart.insurance
- ashley@streetsmart.insurance
- gabrielac@streetsmart.insurance

All requested recipients must pass in the same preflight. If one fails, send nothing and report which validation failed.

## Release gates

- Full Reports 5.0 populations exported, not viewport counts.
- Dashboard and unique-task counts reconciled or visibly flagged.
- Queue membership/version verified.
- Queue scoreboard identifies who was eligible, offered a leg, answered, declined/timed out, or was not offered; answers alone are not used to rank non-answering employees.
- Callback cases reconciled against later calls and EZLynx activity.
- Each callback case hyperlinks the matched EZLynx account and records the same-day Eva/AI inbound-call discussion, later human notes, and all repeated caller attempts.
- Employee duration totals state the source measure: inbound Handle Time and outbound connected Call Length unless a more explicit RingCentral talk-time field is exported.
- Hold and queue-wait findings use explicit RingCentral fields only. Values above 120 seconds are listed separately, and hold is never inferred from Call Length minus Handle Time.
- Policy-change and COI original dates match the trackers.
- Sales Center Assigned Producer remains distinct from account producer.
- Active roster and mailbox allowlist pass.
- Dashboard renders without overflow or clipping.
- Every Google Doc tab is pageless, has no unresolved template tokens or Markdown syntax, and the four approved graphics are present.
- Every overdue task, Sales Center candidate, callback candidate, policy change, COI, submission, and Magellan finding is reconciled into at least one applicable department tab. Cross-department COIs may appear on more than one tab.
- Department tabs use account-level headings and separate detail/evidence paragraphs; reject interleaved chips, split words, and undifferentiated record walls.
- Team-facing overdue-task records omit Task ID and use the bold applicant/account name as the record heading. The Task ID remains in the workbook.
- Policy-change owner/date/age/status lines are bold light-blue callouts. Pending COI status lines are pale-yellow callouts and appear first in each applicable department.
- Send the approved report package by email after recipient preflight. Carlo approved the team-lead Google Chat webhook on 2026-09-01. Send only the reporting date, a concise review checklist, and secured Drive links; do not post raw client-identifying row data in chat. Retrieve the webhook from a runtime secret source and never embed it in the skill, report, scheduler prompt, logs, or ZIP.
- The comprehensive workbook includes every row from each collected source and passes a formula-error scan plus a rendered visual check of every tab.
- Human approves exact subject, body, recipients, and attachments during Test.
- Production remains disabled. Google Chat is enabled only for the approved Test team-lead webhook and only after the report package is complete.
