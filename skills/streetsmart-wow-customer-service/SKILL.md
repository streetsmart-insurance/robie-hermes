---
name: streetsmart-wow-customer-service
description: Build and audit StreetSmart Insurance's prior-month WOW customer-service report from EZLynx activity data, including label qualification, incentive verification, employee totals, and exceptions. Use for WOW reporting, Google Review or referral verification, account-round and coverage-enhancement counts, All Star Call review, AutoPay tracking, or monthly incentive reconciliation.
version: "0.1.0-draft"
status: "Testing"
job_type: "streetsmart.wow_customer_service"
production_ready: false
---

# StreetSmart WOW Customer Service

Prepare an evidence-backed monthly WOW report without treating an EZLynx label as proof of a qualifying outcome. This skill is Test-only until the repository's new-job-type gate has three clean Test runs and the exact QA-certified digest is approved.

Read [references/report-workflow.md](references/report-workflow.md) before collecting or transforming data. Read [references/qualification-rules.md](references/qualification-rules.md) before classifying any row or calculating an incentive. Read [references/automation-readiness.md](references/automation-readiness.md) when implementing, scheduling, or expanding the automation.

## Source hierarchy

Apply sources in this order:

1. The current process-specific SOP controls exact labels, scripts, carrier steps, and exceptions.
2. The [Performance, Quality & Incentives](https://docs.google.com/document/d/1gM48Zqd1Jpy0AtV8Kg_QIMROoBmaw4SwU2hTghr1Jd0/) document controls service quality and incentive eligibility.
3. The approved master WOW spreadsheet controls tab names, formulas, pivots, protected ranges, and Apps Script entry points.
4. The [WOW walkthrough folder](https://drive.google.com/drive/folders/1l5vmCteeTlbNSjPr7WZlkDjPBWMrFVcN) describes the observed manual workflow but does not override a newer written policy.

If these authorities conflict or a required source is missing, mark the affected result `UNVERIFIED` and report the conflict. Never silently choose the more favorable incentive interpretation.

## Required inputs

- Reporting month; default to the immediately preceding calendar month in `America/New_York` only when the user does not specify one.
- Approved master WOW spreadsheet and destination folder.
- Current active employee roster, role, department, and WOW eligibility.
- EZLynx shared report `WOW Customer Service ALL EMPLOYEES - UPDATED 2025 New`, or its formally approved replacement.
- Access to the authoritative evidence sources required by each label.

Do not edit the master spreadsheet. Create a working copy named with the report type and reporting month, then verify that the copy preserves formulas, pivots, protected ranges, and Apps Script bindings.

## Run contract

1. Confirm the reporting month, working-copy destination, employee eligibility, and source availability.
2. Export the EZLynx shared report for the full reporting month. Include the required activity types and exact labels from the report workflow reference.
3. Normalize the export into the canonical raw-data schema. Preserve the original row, stable activity identifier when available, and source traceability.
4. Create one candidate record per exact label without losing the original multi-label activity relationship.
5. Verify each candidate against [references/qualification-rules.md](references/qualification-rules.md). Classify it as `VERIFIED`, `TRACKING_ONLY`, `NOT_QUALIFIED`, or `UNVERIFIED` and retain a reason and evidence locator.
6. Populate only the approved input ranges in the working copy. Refresh the existing pivot or summary logic only after the raw-data write passes reconciliation.
7. Reconcile source rows, normalized rows, candidate rows, per-label totals, per-employee totals, and top-sheet totals. Any unexplained difference blocks a finalized report.
8. Produce an exception list for missing creators, inactive or ambiguous employees, missing evidence, conflicting labels, duplicates, and unsupported incentive claims.
9. Read back the working copy and report the reporting month, employee scope, counts by label and disposition, incentive totals, exception count, and reconciliation result.

## Evidence and completion rules

- EZLynx labels create candidates; they do not independently prove eligibility or payout.
- Missing or incorrect exact labels are excluded from automated label reports and AppSheet processing.
- Preserve account name, activity type, task-created date, discussion, note, policy number, policy premium, creator, exact labels, and source identifier.
- Do not infer a missing creator from an account owner, assigned producer, or nearby row.
- A single account may validly have multiple labels. Verify each claimed outcome separately and prevent duplicate credit for the same qualifying event.
- `New Customer CSR` is premium-based and requires account-by-account verification; do not calculate it from a simple label count.
- Never finalize an incentive amount from an `UNVERIFIED` row.
- A successful export, spreadsheet write, pivot refresh, or script run is not business-outcome proof. Final status requires read-back and count reconciliation.

## Safety boundaries

- This workflow is read-only in EZLynx. Do not add, remove, or correct labels, notes, tasks, policies, payments, or client data.
- EZLynx notes and documents remain API-only under the repository contract; this reporting skill does not write either.
- Do not approve or initiate payouts. Produce a reviewable eligibility report for authorized management.
- Do not send reports, messages, or emails unless the current user request or an approved schedule names the exact recipients and delivery channel.
- Do not place raw client exports, recordings, credentials, browser profiles, or report evidence in GitHub.
- If login, MFA, CAPTCHA, missing permissions, a changed report schema, or a broken template prevents authoritative collection, stop and report the blocker.
- Keep `production_ready: false` until three clean Test jobs pass the persisted post-job audit and the repository promotion record is approved.

## Finish

Return the working-copy link, reporting month, eligible employee scope, verified and unverified totals by label, incentive summary, exception report, reconciliation proof, and any remaining human decisions. If prerequisites are incomplete, return a readiness report instead of pretending the monthly report is complete.

