# StreetSmart Accountability Cloud Runbook

## Target architecture

The authoritative runner belongs on the existing private Google Cloud VM,
behind IAP and without a public SSH port. Deployment follows the repository's
immutable release process:

`feature branch -> CI -> immutable archive -> hermes-test-01 -> three clean Test runs -> Carlo approval -> same digest to hermes-poc-01`

The three scheduled read-only job types are:

| Job | Eastern schedule | Purpose |
|---|---|---|
| `accountability.daily` | Weekdays at 5:00 PM | Calls, queues, callbacks, tasks, urgent service trackers, Magellan alerts |
| `accountability.weekly` | Friday at 5:15 PM | Employee/department rollup, Retention, Submission, Gmail, AppSheet, all trackers |
| `accountability.monthly` | First of month at 8:00 AM | Trends, recurring exceptions, verified case studies and churn evidence |

The local Codex tasks currently use 5:00 PM for the weekly report. The cloud
job defaults to 5:15 PM so the final weekday source exports have time to land.
Change either only after the intended cutover is confirmed.

## Cloud connection inputs

No credentials belong in Git, Chat, report artifacts, or the connection
manifest. Store secret values in Google Secret Manager and expose only the
minimum runtime environment references.

### RingCentral

Choose one:

1. Schedule Detailed Call Log and queue reports to
   `robie@streetsmart.insurance`; the cloud Gmail identity downloads the CSVs.
2. Configure a read-only RingCentral application with client ID, client secret,
   and JWT stored in Secret Manager.

Required evidence includes call/session ID, every call leg, direction,
from/to, extension/queue, result, start time, duration, queue/hold time, and
connected outbound callbacks.

### EZLynx

The current cloud path uses the canonical authenticated browser profile and
Secret Manager references already used for session refresh. Test must prove
exports for Tasks, Activities, Sales Center, Retention Center, and Submission
Center. A failed or stale export produces `UNVERIFIED`, never a favorable score.

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

### AppSheet

Preferred order:

1. Read the underlying Google Sheets tables when they are the authoritative
   AppSheet data source and the cloud service identity already has access.
2. Use the read-only AppSheet `Find` integration for tables not available as
   Sheets.

The AppSheet API requires an Enterprise plan, an enabled inbound API, an App
ID, and an unexpired Application Access Key. Store the key in Secret Manager.
The implementation exposes no Add, Edit, or Delete method.

### Google Sheets trackers

Share only the required spreadsheets with the cloud service identity. The
connection manifest maps every tracker key to a normalized export path. Each
finding keeps its tracker name and source row.

## Test activation

1. Install the reviewed immutable release on `hermes-test-01` using the official
   Test deployment workflow.
2. Create the Test-only manifest from
   `deploy/accountability/connection-manifest.example.json` under
   `/opt/streetsmart-hermes-test/accountability/`.
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
