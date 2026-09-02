# Google Doc output

Create a native Google Doc titled `StreetSmart Daily Accountability — Complete Evidence Report — YYYY-MM-DD`.

## Required native tabs

1. Agency Overview
2. Personal Lines
3. Commercial Lines
4. Trucking & Transportation
5. Operations
6. Executive & Unverified
7. Calls & Queues
8. Policy Changes & COIs
9. Sales Center — All
10. Overdue Tasks — All
11. Submissions & Magellan
12. Validation & Sources

Set each tab to pageless. Use native title, heading, date, person, and link elements when available. Do not emit Markdown formatting characters in the finished document.

Each department tab is a complete, standalone team-lead view. Put the department snapshot, queue metrics, employee phone activity, callback candidates, policy changes, COIs, overdue tasks, Sales Center no-touch candidates, Submission Center exceptions, Magellan sad or at-risk calls, and the daily review checklist on that one tab. When a category has no matched rows, say so explicitly instead of omitting the section.

Assign overdue tasks, Sales Center candidates, employee phone rows, and callbacks from the active AppSheet roster department. Preserve the policy-change tracker's Personal, Commercial, and Trucking group boundaries. A COI with assignees from multiple departments appears on every applicable department tab. Reconcile every normalized source population into at least one department tab; duplicates caused by legitimate cross-department COI ownership are allowed and must be explainable.

Use a readable account-by-account card rhythm: account or caller as a native Heading 2, one short labeled detail line, and a separate evidence or current-note paragraph. Group overdue tasks and Sales Center candidates by responsible owner. Do not compress long evidence into interleaved one-line strings, and do not allow native date or person elements to split surrounding words.

For overdue tasks, use the applicant or account name as the bold native record heading. Do not display the EZLynx Task ID in the team-facing Google Doc; retain it in the comprehensive workbook for evidence and reconciliation.

For policy changes, make the assigned producer, request-created date, days open, and status one bold light-blue status line. Keep line of business, carrier, policy, change-request ID, and current note on separate readable lines.

Within every department's COI section, show pending/actionable COIs first and completed or closed history second. Use a bold pale-yellow status line for pending COIs. Route pending COIs to Commercial or Trucking from the responsible producer/CSR or renewal owner in the tracker and current notes. When responsibility crosses departments, place the COI on every applicable tab and explain the duplication in validation.

Agency-wide tabs contain every normalized exception and every nonblank tracker row. Link the comprehensive workbook for full raw exports.

Insert the approved 16:9 graphics at a readable width:

- Executive summary in Agency Overview
- Personal Lines drill-down in Personal Lines
- Queue performance in Calls & Queues
- Department backlog in Overdue Tasks — All

Read back the entire document before delivery. Confirm all 12 tabs exist, every tab is pageless, no private-use template tokens remain, the four figures exist, and no `##`, `**`, or Markdown pipe-table syntax remains.
