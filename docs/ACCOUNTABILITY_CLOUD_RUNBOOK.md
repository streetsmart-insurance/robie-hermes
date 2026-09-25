# StreetSmart Accountability Cloud Runbook

## Target architecture

The authoritative runner belongs on the existing private Google Cloud VM,
behind IAP and without a public SSH port. Deployment follows the repository's
immutable release process:

`feature branch -> CI -> immutable archive -> hermes-test-01 -> three clean Test runs -> Carlo approval -> same digest to hermes-poc-01`

The three scheduled read-only job types are:

| Job | Eastern schedule | Purpose |
|---|---|---|
| `accountability.daily` | Weekdays at 9:00 AM | Prior-business-day calls, queues, callbacks, tasks, urgent service trackers, Magellan alerts |
| `accountability.weekly` | Friday at 5:15 PM | Employee/department rollup, Retention, Submission, Gmail, AppSheet, all trackers |
| `accountability.monthly` | First of month at 8:00 AM | Trends, recurring exceptions, verified case studies and churn evidence |

The local Codex tasks currently use 5:00 PM for the weekly report. The cloud
job defaults to 5:15 PM so the final weekday source exports have time to land.
Change either only after the intended cutover is confirmed.

On the dedicated VM `streetsmart-accountability-prod`, weekdays are three separate units: 09:00 America/New_York publishes the department Doc, emails leadership, and posts Team Lead Chat for the previous business day (`run_daily_accountability_vm.sh` omits `--date`, so `production_main` keeps `get_previous_business_day()`); 17:00 collect runs only `run_source_collection_vm.sh` (`production_main --skip-if-prepared --date` set to that same Eastern calendar day, no publish and no Chat); 17:05 Team Lead Chat EOD posts a short Chat wrap-up for that Eastern calendar day to the same Team Lead webhook (`accountability-team-lead-chat-webhook`, space `spaces/AAQAHYP7Ezg`) and does not send leadership email or rewrite the Doc. `--skip-if-prepared` reuses a snapshot only for the date it was given. Omitting `--date` on the 17:00 unit prepared the prior business day and left EOD with no same-day snapshot.

## Cloud connection inputs

No credentials belong in Git, Chat, report artifacts, or the connection
manifest. Store secret values in Google Secret Manager and expose only the
minimum runtime environment references.

### RingCentral

Choose one:

1. Schedule Detailed Call Log and queue reports to
   `robie@streetsmart.insurance`; the cloud Gmail identity downloads bounded
   CSV or macro-free XLSX attachments and checksum-binds the evidence.
2. Configure a read-only RingCentral application with client ID, client secret,
   and JWT stored in Secret Manager.

Required evidence includes call/session ID, every call leg, direction,
from/to, extension/queue, result, start time, duration, queue/hold time, and
connected outbound callbacks.

### EZLynx Five / Reports 5.0

The current cloud path uses the canonical authenticated browser profile and
Secret Manager references already used for session refresh. The weekly job can
collect Submission Center evidence directly when
`collection.ezlynx_submission_center.enabled` is true. That collector is
read-only: it sets All Submissions / Streetsmart Insurance, uses 100 rows,
verifies Status ascending with a non-closed first row, follows every page until
the first closed record, and requires both the live `overdue` class and
`rgb(211, 47, 47)` before applying the day-31 rule. It captures direct links and
groups qualifying records by assigned producer. Cached rows and capped exports
are never completeness proof.

Tasks, Activities, Policy Changes, COIs, Sales and Retention must come from
verified EZLynx Five / Reports 5.0 mappings. The legacy
`EZLynxReportPortal/SavedReports` catalog is not an automatic fallback. The
connection manifest pins `version=5.0`, keeps legacy fallback false and fails
closed while any required mapping is unverified. Verified mappings currently
include Activity `Activity Summary` (#25979) and `Activity Detail` (#546),
Policy Transaction `Policy Change Request Summary` (#25997) and `Change Request
Detail` (#492), Sales `Sales Performance Dashboard` (#25985) and `Sales Center
Detail` (#3469), and Retention `Renewal Summary` (#25991), `Premium Change Detail
(EZLynx Data)` (#3688), and `Renewal Detail` (#494). COIs use `Activity Detail`
(#546) with `Activity Labels Activity Labels contains Certificate of Insurance`,
then reconcile the result to the direct account Activity link in the COI tracker.
Audits use `Activity Detail` (#546) with the same field containing `Audit`, plus
`Policy Transaction Detail` (#489) and the latest account Activity discussion.

A failed browser read or stale export produces `UNVERIFIED`, never a favorable
or adverse employee result. Sales open opportunities are grouped by producer
and flagged when Reports 5.0 shows no touch beyond
`rules.sales_untouched_days` (five days by default). The exception preserves
opportunity, stage, last-touch age, note quality, and source row. Submission
Center remains a separately verified live-center collector and flags only non-terminal records whose
live red Quote Due Date is more than 30 calendar days old. Producer cleanup
emails are a separate gate: the collector records
`email_delivery_enabled=false`, and no employee email may be sent without
current recipient verification and an immediate user confirmation.

EZLynx is the account-level source of truth. RingCentral, Gmail, Magellan, and
department trackers are external evidence to reconcile to the EZLynx account,
owner, tasks, activities, and notes. Policy changes are escalated after seven
days and classified as carrier-, client-, StreetSmart-, or unclear-blocked.
Expiration and pending COI/endorsement exceptions must show the producer/CSR,
documented outreach, blocker, and next action. Missing EZLynx identity is
reported as `UNVERIFIED`.

### Magellan

Configure scheduled report delivery to the Robie mailbox when possible.
Otherwise, use the canonical authenticated cloud browser to export a bounded
date range. The normalized file contains call ID/phone, timestamp, sentiment,
tags, and transcript/recording reference; report output does not include full
transcripts by default.

### Gmail employee accountability

Robie's existing mailbox token covers only the Robie mailbox. Agency-wide
employee reporting requires Google Workspace administrator approval for a
dedicated read-only identity with domain-wide delegation, an allowlist of
employee mailboxes, and Gmail metadata scope. The service reports thread state
and age; it does not publish message bodies. Shared inboxes, spam, bulk mail,
auto-replies, internal-only threads, PTO, and approved delegations need written
exclusion rules.

Google Workspace Admin has granted the StreetSmart Hermes domain-wide
delegation client `gmail.metadata`, `gmail.readonly`, and `gmail.send`.
Authorization is not runtime proof. Before enabling the collector,
`hermes-test-01` must impersonate every approved active employee mailbox from
the approved employee roster and read back the exact delegated profile. Any
missing roster email, non-StreetSmart email, profile mismatch, or mailbox
failure stops the Test run.

The employee collector requests only
`https://www.googleapis.com/auth/gmail.metadata`. Its allowlist is derived from
the active employee role registry; `ACCOUNTABILITY_GMAIL_USERS` is a
compatibility fallback, not the agency-wide source of truth. Output contains
per-mailbox counts, reply ownership, ages, and hashed evidence IDs; it does not
contain message bodies or subjects. `gmail.readonly` is reserved for
`robie@streetsmart.insurance` and scheduled report attachments. Although
`gmail.send` is authorized, email and Google Chat delivery remain disabled
until Carlo approves the exact digest and recipients.

### AppSheet

Preferred order:

1. Read the underlying Google Sheets tables when they are the authoritative
   AppSheet data source and the cloud service identity already has access.
2. Use the read-only AppSheet `Find` integration for tables not available as
   Sheets.

The AppSheet API requires an Enterprise plan, an enabled inbound API, an App
ID, and an unexpired Application Access Key. Store the key in Secret Manager.
The implementation exposes no Add, Edit, or Delete method.

For the supplied backing spreadsheet
`1dFmE_J-YM9ua7v_B2UDgHHFr6BofIpIuU9qEXXtoZnA`, configure explicit tab,
range, and column allowlists under `google_sheets`. The collector persists only
those columns. SSNs, compensation, home addresses, personal email, and other HR
fields must never be included. The Employees allowlist can generate the approved
employee-to-role registry used by every reporting tier.

### Report delivery

Delivery is disabled until destinations are explicit. Configure existing Google
Chat space names under `delivery.chat_spaces`; the job posts as the Robie Chat
app, splits long reports safely, and independently reads every created message
back before the job can become COMPLETE. It never creates a new space.

Email uses `robie@streetsmart.insurance` through keyless Workspace delegation.
Configure recipients separately for daily, weekly, and monthly reports. The
required scopes are `gmail.send` and `gmail.metadata`; the latter supports fresh
sent-mail read-back. Department-lead addresses must be supplied explicitly and
are never inferred from names. A delivery failure fails the job even when the
local report artifact was generated.

### Role/accountability boundary

The employee registry maps each person to an approved role. Producers receive
outbound, Sales Center, Submission Center, and follow-up facts—not an inbound
queue penalty unless the registry explicitly assigns that queue duty. Account
managers/CSRs receive direct/assigned queue calls, callback, EZLynx task,
renewal, change/COI, and email facts. Managers receive queue and backlog
oversight facts. Magellan quality is labeled `INSUFFICIENT_SAMPLE` below the
configured minimum call count, so a high sentiment score on two calls cannot be
presented as top performance.

### Google Sheets trackers

Share only the required spreadsheets with the cloud service identity. The
Test VM ADC identity (typically
`robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com`)
must present `https://www.googleapis.com/auth/spreadsheets.readonly` and,
for Shared Drive-hosted sheets,
`https://www.googleapis.com/auth/drive.readonly`.
`ACCESS_TOKEN_SCOPE_INSUFFICIENT` / 403 is fail-closed: never invent
producer emails. See [OVERDUE_SUBMISSION_TEST_FIXES.md](OVERDUE_SUBMISSION_TEST_FIXES.md).
The
connection manifest maps every tracker key to a normalized export path. Each
finding keeps its tracker name and source row.
The COI source is spreadsheet `1LXer2SjkQGvj1dsaRGmFnkB_hGBQeJolagw1oFbycZY`.
Its collector resolves `{month}!A:H` in Eastern time and persists only Date,
Date COI was Requested, Profile, Link, Requirements/Endo, assigned agent,
Status, and Date Completed. The tracker remains authoritative for the worklist;
the direct EZLynx account Activity page is authoritative for evidence and the
latest documented outcome.
The Missed Calls tracker is a RingCentral reconciliation source and is not
added to call totals a second time. Pending Payouts remains available as an
optional definition but is excluded from the initial active manifest.

Employee alerts are drafts in Test. Enabling delivery requires a separately
approved recipient map and delivery connection; Test never messages employees.

## Test activation

Until separately approved, accountability is run manually every weekday in
Test only. The weekday scheduler wakes the Test work item; it is not permission
to deploy, run, or deliver from Production. Production remains prohibited.

1. Install the reviewed immutable release on `hermes-test-01` using the official
   Test deployment workflow.
2. Create the Test-only manifest from
   `deploy/accountability/connection-manifest.example.json` under
   `/opt/streetsmart-hermes-test/accountability/`.
   Keep leadership delivery and producer cleanup email disabled.
3. Run the redacted connection check. It must not print secret values.
4. Install the three recurring schedules into the Test Job database.
5. Run each reporting mode against non-production fixtures, followed by fresh
   artifact verification.
6. Record three clean Test jobs and post-job audits for the new reporting job
   type before Production promotion.
7. Carlo approves the exact release digest and the same archive is promoted to
   `hermes-poc-01` through the official Production installer.

## Cutover rule

Once the cloud jobs have produced three verified reporting cycles, disable the
local Codex recurring tasks to prevent duplicate reports. Cloud remains the
authoritative scheduler; local execution is retained only as a manual recovery
path.
