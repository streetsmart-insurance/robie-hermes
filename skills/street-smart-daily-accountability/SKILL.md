---
name: street-smart-daily-accountability
description: Run StreetSmart Insurance's prior-business-day accountability audit, create department reports and a visual dashboard, reconcile calls/tasks/policy changes/COIs/Sales Center, and deliver the approved digest through the gated Test-to-Production release. Use for daily accountability runs, team-lead reporting, callback audits, overdue-work analysis, or the 6:25 AM digest.
---

# StreetSmart Daily Accountability

Produce an evidence-backed internal management package for the previous business day. Production requires Carlo's authorization plus the repository's immutable Test-to-Production promotion workflow; never patch a live server directly.

Read [references/report-contract.md](references/report-contract.md) before collecting data or sending a report.
Read [references/google-doc-output.md](references/google-doc-output.md) before creating or validating the Google Doc.
Read [references/webhook-delivery.md](references/webhook-delivery.md) before configuring or using the approved team-lead Google Chat webhook.

## Required outcome

Generate one agency summary, a pageless Google Doc with native department tabs, evidence CSVs, a visual dashboard presentation, and a comprehensive multi-tab Excel workbook containing the complete row-level source evidence. The package must answer:

- Which clients may not have received a callback or email response?
- How did Personal, Commercial, Trucking, and lead queues perform?
- Who answered, received voicemails, called back, or has verified missed routing offers?
- How much measured connected/talk time did each employee handle through direct inbound, queue inbound, and outbound calls?
- Which callers waited in queue or were placed on hold for more than 120 seconds, and which queue or employee owned the verified leg?
- Who owns overdue tasks, how old are they, and which departments have the largest backlog?
- When were policy changes and COIs requested, how old are they, and who or what is blocking them?
- Which open Sales Center opportunities have no verified touch beyond five days, what is the lead source, and who is the Sales Center-assigned producer?

Within every agency and department tab, group the readable findings in this order: Phone & Queue Service, Client Follow-Up, Policy Service, Sales, Tasks & Activities, COIs & Submissions, Customer sentiment (SAD) — Magellan, and Validation. Keep the complete row-level source data in the workbook instead of turning the management narrative into an undifferentiated record wall.

## Evidence rules

- Use EZLynx Reports 5.0 exports, never legacy reports.
- Start the automated run Monday–Friday at 6:25 AM America/New_York. Report only the prior business day's 9:00 AM–5:00 PM America/New_York activity; Monday uses Friday.
- Accept RingCentral's generic email subject `Scheduled Reports from RingCentral` only when an attachment is explicitly labeled `Yesterday Calls`. Collect Queues, Users, and Calls XLSX attachments as one evidence bundle. Validate the date embedded in call rows or filters against the requested prior business day; never accept the newest weekend message merely because it arrived most recently.
- Deduplicate Activity Detail history on Task ID and retain the newest evidence row.
- Use the active AppSheet employee roster for team membership. Preserve EZLynx Department separately as the workstream.
- Separate RingCentral parent calls from routing legs. Zero pickups do not prove a missed offer.
- Define a routing leg as one destination attempt inside a parent call. A missed routing leg is not automatically a missed client call and is not employee accountability unless RingCentral proves that the employee was an eligible queue member and was actually offered that leg.
- Keep requested-producer/direct-extension routing separate from queue routing. A destination name may show whom the caller requested; it does not by itself prove that person owned the callback.
- Build a queue scoreboard from a dated queue-membership snapshot plus verified offered, answered, declined/timed-out, voicemail, and abandoned legs. Do not infer nonparticipation from queue answers alone.
- Close a callback candidate only with a later connected outbound call to the same number or a later inbound call answered by a human. AI-only answers do not close it.
- For every callback candidate, search the caller number in EZLynx, hyperlink the matched account, inspect the Activity tab for Eva/AI inbound-call discussions and later notes, and preserve every same-day attempt. No later note is evidence for review, not automatic proof of employee fault.
- Report direct-inbound, queue-inbound, outbound, and total measured connected/talk seconds by employee. Use RingCentral Handle Time for answered inbound legs when present and connected Call Length for outbound legs; label the measure and source field instead of calling an inferred value exact talk time.
- Flag `HOLD_REVIEW` only when RingCentral supplies an explicit hold-duration field above 120 seconds. Separately flag `QUEUE_WAIT_REVIEW` when an explicit queue-wait/time-to-answer field is above 120 seconds. Never derive hold time from Call Length minus Handle Time.
- Put all explicit `HOLD_REVIEW` and `QUEUE_WAIT_REVIEW` records above 120 seconds at the top of Phone & Queue Service before the queue scorecard and remaining call detail.
- For every hold/wait exception retain caller, phone, parent session ID, leg timestamp, queue, destination/answering employee, exact measured seconds, source field, callback disposition, and EZLynx account link. If ownership exists only at parent-call level, label the owner `UNVERIFIED`.
- Use Sales Center Assigned Producer. Do not present the producer on the customer account as the Sales Center owner.
- Collect Submission Center through the approved read-only persistent-browser audit when Reports 5.0 offers no scheduled attachment. Preserve its All Submissions, agency, 100-row, Status-order, first-closed-row, live-red-state, day-31, link, and count-reconciliation assertions. A login/reset page is `UNVERIFIED`, never zero pending submissions.
- Collect Magellan through its own persistent Chrome profile and CDP port, never the EZLynx profile or Carlo's daily browser. Retrieve `magellan-username` and `magellan-password` from Secret Manager, verify the authenticated dashboard, collect only the requested prior-business-day call metadata/sentiment/tags, and exclude transcripts. Expired authentication, an unverified target-date boundary, or incomplete pagination is `UNVERIFIED`, never zero sad calls.
- Keep raw phone values only in the restricted reconciliation records. Any `caller_phone_masked` field used by the management digest must expose only the last four digits (`***-***-1234`) and must not retain raw `from_number` or `to_number` fields.
- Keep full client names and phone numbers in the authorized internal report.
- Include all source rows in dedicated workbook tabs for Activity Detail, overdue Activity history, Sales Center, and RingCentral, plus complete nonblank policy-change and COI tracker rows. Do not reduce the Excel deliverable to exception-only summaries.
- Build the Google Doc with native tabs for Agency Overview, Personal Lines, Commercial Lines, Trucking & Transportation, Operations, Executive & Unverified, Calls & Queues, Policy Changes & COIs, Sales Center — All, Overdue Tasks — All, Submissions & Magellan, and Validation & Sources.
- Set every Google Doc tab to pageless. Use native title and heading styles; never leave Markdown markers such as `##`, `**`, or pipe tables in the finished Doc.
- Put every normalized accountability record and every nonblank tracker row in the Google Doc. Link the comprehensive workbook for full raw exports that are too large to paste into a readable Doc.
- Reuse the approved high-level dashboard graphics in the relevant Google Doc tabs: executive summary, department backlog, queue performance, and Personal Lines drill-down.
- Label missing, stale, conflicting, or out-of-window evidence `UNVERIFIED`; do not invent exact dates or owners.
- Exclude Carlo from employee phone accountability while retaining client callback cases that routed through his extension.

## Delivery boundary

Sending is authorized only by the current user request or an active scheduled automation that names the exact recipients. Before every send, verify each mailbox against the approved active roster and Test Gmail profile. One inactive, missing, or unverified requested recipient blocks the send and produces a clear exception report.

Do not read employee email bodies. Use Gmail metadata for approved employees and restrict Gmail readonly content access to the reporting mailbox. Google Chat delivery is approved only for the configured team-lead incoming webhook and only for the concise link-only digest defined in the webhook reference after the same release and recipient gates pass.

Deliver the package by link after the all-recipient preflight passes. The email must link the pageless Google Doc, comprehensive Google Sheet/workbook, visual dashboard, runbook, and packaged skill. Ask Jake and the team leads to scrutinize the findings and return corrections or remarks.

After the email delivery succeeds, send the same reporting date and secured Drive links to the approved team-lead Google Chat space with `scripts/send_team_lead_webhook.py`. Retrieve the webhook only from a runtime secret source. Never place it in the skill, report, automation prompt, command arguments, logs, or ZIP.

## Implementation assets

- `robie_job_engine/department_accountability.py`: unique overdue tasks and department offender rankings.
- `robie_job_engine/center_audits.py`: Sales Center assignment, lead source, aging, and Activity joins.
- `robie_job_engine/operational_trackers.py`: original tracker request dates and age.
- `robie_job_engine/magellan_collection.py`: read-only, target-dated Magellan sentiment collection through the dedicated persistent browser.
- `scripts/export_daily_accountability_details.py`: evidence CSV generation.
- `scripts/run_daily_accountability_vm.sh`: source of truth for the dedicated VM wrapper at `/opt/streetsmart-daily-accountability/scripts/run_daily_accountability_vm.sh`. util-linux `flock -w` must be integer seconds (`900`, not `15m`).

Stop and report the exact blocker when a source export, active-roster match, mailbox proof, or dashboard/export reconciliation fails. Never turn incomplete evidence into an employee finding.
