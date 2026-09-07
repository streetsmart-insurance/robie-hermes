---
name: "ezlynx-overdue-submission-reports"
description: "Review every assigned producer in the StreetSmart EZLynx Submission Center, identify red open submissions whose Quote Due Date is more than 30 days old, and send verified individualized close-out reports."
version: "0.1.0-draft"
status: "Testing"
job_type: "ezlynx.overdue_submission_reports"
production_ready: false
---

# EZLynx Overdue Submission Reports

## Activation and hard stops

- An installation, deployment, or test request does not authorize sending email.
- Run the workflow only when Carlo explicitly requests the overdue-submission review or an approved schedule starts it.
- If EZLynx requires login, MFA, or CAPTCHA, stop before classifying records or sending email and report the blocker.
- If Gmail delivery or authoritative recipient lookup is unavailable, stop before sending. Never guess an address.
- Do not change submission statuses. The assigned producer must review and close each record.

## Qualification rules

- Open `https://app.ezlynx.com/web/submission-center/overview/submissions`.
- Set **Submissions by assigned producer** to **Streetsmart Insurance** so every assigned producer is included.
- Include only submissions whose **Quote Due Date is displayed in red/overdue** and is **more than 30 calendar days before the run date**.
- Use date-only calendar arithmetic: `run date - Quote Due Date > 30 days`. A record exactly 30 days overdue does not qualify; it qualifies beginning on day 31.
- Exclude every submission with status **Closed - Not Sold** or **Closed - Bound**.
- Treat all other statuses as open. Do not hard-code an exhaustive list because EZLynx may add statuses.
- Group results by **Assigned Producer**, not Created By.
- Deduplicate by the full submission URL.

## Collect the report

For every qualifying submission, capture:

1. Submission title and direct link
2. Applicant
3. Assigned producer
4. Current status
5. Quote due date
6. Effective date

Review every results page. Prefer the largest available page size. Confirm the red overdue state from the live UI styling or overdue marker; do not infer it solely from the calendar date. Then apply the more-than-30-day calculation to the displayed Quote Due Date. A submission must pass both checks.

## Resolve recipients

Verify each producer's active work email using EZLynx **Admin -> Manage Users** or another authoritative current staff directory. Resolve each producer individually and never guess an address. Do not email a producer with no qualifying records.

## Email each producer

Send one individualized email per producer with:

- Subject: `Action Required: [COUNT] EZLynx submission(s) over 30 days overdue`
- A statement that the listed records are shown in red, are more than 30 days past their Quote Due Date, and have not been closed.
- Instructions to open the [EZLynx Submission Center](https://app.ezlynx.com/web/submission-center/overview/submissions), choose **My Submissions**, and sort by **Quote Due Date**.
- Instructions to review every item and select **Closed - Not Sold** or **Closed - Bound**, as appropriate.
- A link to [Step 7 of the Submission Center SOP](https://sites.google.com/streetsmart.insurance/siteswiki/employee-hub/ezlynx/submission-center#h.lnepijyzf63).
- The submission link, applicant, current status, quote due date, and effective date for every item.
- A concise request to complete the close-outs as soon as possible.
- Sign the email as Carlo.

Use Gmail for delivery. An explicit request to run this workflow, or its approved schedule, authorizes sending only these individualized reports to the verified assigned producers.

## Finish

Report the total qualifying submissions over 30 days overdue, counts by producer, statuses found, and confirmed email delivery results. If there are no qualifying submissions, do not send staff emails; report that the over-30-day queue is clear.

