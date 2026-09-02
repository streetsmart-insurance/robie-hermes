# Daily Accountability Report Handoff

## Purpose

This workflow produces an evidence-backed prior-business-day report for agency leaders. It combines phone and queue service, client follow-up, policy service, Sales Center, overdue tasks and activities, COIs and submissions, sentiment findings, and validation status.

## Schedule and environment

- Run every weekday at 9:00 AM America/New_York.
- Report the previous business day; Monday reports Friday.
- Use the Test environment until an authorized administrator explicitly approves Production.
- Restrict operational data, client identifiers, employee findings, recipient addresses, and delivery secrets to approved private runtime storage and access-controlled deliverables.

## Inputs

- RingCentral call-detail and queue exports, including parent calls and routing legs.
- EZLynx Reports 5.0 exports for activities, policy changes, Sales Center, and submissions.
- Approved policy-change and COI trackers.
- Magellan sentiment evidence.
- Active employee roster and department mapping.
- Gmail metadata for approved mailboxes, with message bodies excluded from employee-accountability analysis.

## Required outputs

1. A pageless Google Doc with an agency overview and one complete tab per department.
2. A comprehensive workbook containing all row-level evidence and validation logs.
3. A concise visual dashboard.
4. A source and release-gate validation summary.
5. Link-only email and, when privately authorized, a link-only team-lead Google Chat message.

## Validation gates

- Confirm the reporting window and previous-business-day calculation.
- Confirm all Reports 5.0 exports are complete populations rather than viewport counts.
- Reconcile RingCentral parent calls, routing legs, callbacks, voicemail, queue membership, and explicit hold or queue-wait values.
- Flag hold or queue-wait exceptions only from explicit source fields above 120 seconds.
- Verify callback candidates against later calls and the matched EZLynx account Activity history.
- Preserve the original request date, current age, status, owner, blocker, and next action for policy changes and COIs.
- Use Sales Center Assigned Producer rather than the producer recorded on the customer account.
- Reconcile unique overdue-task counts to department totals and retain full evidence in the workbook.
- Label missing, conflicting, stale, or unverified evidence clearly. Never turn incomplete evidence into an employee finding.
- Validate every requested recipient before sending. One failed recipient blocks the entire send.

## Deployment notes

The reusable implementation contract lives in `skills/street-smart-daily-accountability/`. Keep recipient addresses, employee exclusions, webhook values, internal Drive links, account identifiers, client data, and current approval state outside Git. Supply those values through private runtime configuration and an approved secret manager.

Before enabling a cloud schedule, run the skill manually in Test, compare the Doc and workbook totals, verify the dashboard renders, and obtain approval for the exact recipients and delivery text. The scheduled job should fail closed when any source, roster, recipient, or delivery preflight fails.
