# Production accountability orchestrator handoff

## Outcome

The weekday 9:00 AM Eastern service must run `scripts/run_accountability_job.py daily`.
It must never call `scripts/run_productivity_pipeline_automated.py` directly.
The new entry point creates one durable occurrence per reporting period, runs
`AccountabilityReportWorker`, and requires independent verification before the
Job can become `COMPLETE`. Monday and post-holiday runs select the prior
business day. A repeat invocation for the same period reuses the same Job.

## Required Production connections

The private manifest at
`/opt/streetsmart-hermes/accountability/connection-manifest.json` must declare:

```json
{
  "safety": {"environment": "Production"},
  "rules": {
    "daily_reporting_period": "previous_business_day",
    "holiday_calendar": "US-FEDERAL",
    "require_complete_evidence": true,
    "production_required_connections": [
      "ringcentral", "ezlynx", "gmail", "magellan", "google_sheets", "delivery"
    ]
  }
}
```

The activation refuses to enable the timer until all six connections report
ready. This is configuration readiness, not proof that a run succeeded.

- RingCentral: scheduled `Yesterday Calls` workbooks delivered to Robie's
  reporting mailbox, with Calls, Users, and Queues evidence and the embedded
  target date.
- EZLynx: scheduled Reports 5.0 attachments for Tasks/Activities, Policy
  Changes, COIs, Sales Center, and Retention. The scheduled-report collector is
  now wired into the worker. Submission Center may use Robie's dedicated,
  read-only persistent browser and can be enabled for `daily` and `weekly`.
- Robie EZLynx identity: keep the existing dedicated account, Secret Manager
  references, persistent profile on port 9222, and Robie MFA mailbox path. Do
  not create a new identity or substitute Carlo's credentials/session.
- Magellan: Robie's dedicated Secret Manager identity and persistent browser on
  port 9223.
- Gmail: delegated service-account signer, metadata allowlist from the active
  employee roster, and readonly access only to Robie's report mailbox.
- Google Sheets: approved employee roster plus policy-change and COI trackers,
  restricted to explicit column allowlists.
- Delivery: verified recipient allowlist and approved team-lead Chat
  destination. Delivery stays off when any required source is missing,
  incomplete, stale, or unreconciled.

## Release and activation

1. Merge the reviewed feature branch after CI and mandatory Test evidence.
2. Build one immutable release and promote that exact digest through the
   protected Production workflow.
3. Populate the private Production manifest and environment files from Secret
   Manager; never commit secret values.
4. Run the `Activate Production accountability schedule` workflow from the
   exact Production commit with its required confirmation phrase.
5. Read back the timer and service command. The timer must be enabled/active at
   `Mon..Fri 09:00 America/New_York`, and the service must call
   `run_accountability_job.py daily` as `streetsmart-hermes`.
6. Trigger one bounded no-send run first. A missing source must exit nonzero.
   After complete-source and recipient verification, enable delivery and run
   the same immutable release again for the next unsent reporting period.

## Local-to-cloud parity gate

Production delivery is prohibited unless the Test artifact matches the approved
local report contract for the same prior-business-day source package. The gate
must verify all of the following, not merely that a report file exists:

- the same Eastern reporting date and 9:00 AM–5:00 PM business-hours window;
- the same approved active-employee roster and department routing;
- complete RingCentral parent calls, routing legs, direct and queue sources,
  callbacks, queue metrics, talk time, and explicit hold/queue-wait evidence;
- EZLynx Reports 5.0 Tasks, Activities, Policy Changes limited to 90 days,
  COIs, Sales Center assigned producer, Retention, Audits, and live Submission
  Center evidence, plus Magellan and the approved trackers;
- the pageless department-first Google Doc, complete multi-tab Excel workbook,
  and all-data Google Sheet, with the same row counts and department totals;
- successful source checksum validation, artifact read-back, recipient
  preflight, and delivery receipt verification.

Missing, stale, partial, differently dated, or unreconciled evidence must make
the Job fail. It must never send a smaller cloud report as if it were equivalent
to the approved local report.

## Retention follow-on

The June–August 2026 lost-customer review is a separately gated backfill. It
uses Robie's existing EZLynx identity and the supplied Lost Customers workbook,
then the monthly job reviews the complete prior month. It must reconcile Reports
5.0 Policy Transaction Detail, Retention Center, Tasks/Activities, callbacks,
texts/email metadata, policy changes, submissions, and Magellan sentiment. It
is read-only, non-accusatory, and fail-closed. The two required workbook tabs
are `3-Month Account Review` and `3-Month Executive Analysis`. This backfill
must not delay, weaken, or silently fill gaps in the daily report.
