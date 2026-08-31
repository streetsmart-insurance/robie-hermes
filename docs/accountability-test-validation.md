# Accountability Test validation

Status: implementation candidate only. Production delivery and employee alerts
remain disabled. Missing, stale, partial, ambiguous, or unauthenticated evidence
must remain `UNVERIFIED`.

## What the Test build collects

The scheduled Job Engine can now collect two distinct Gmail evidence classes:

1. RingCentral XLSX bundles from the dedicated Robie report mailbox. These use
   the `ROBIE_DAILY_CALLS` and `ROBIE_WEEKLY_CALLS` labels and validate required
   worksheets, columns, current users, current queues, and queue membership.
2. Explicitly configured CSV or JSON attachments for EZLynx and Magellan. Each
   source requires an exact sender allowlist, a subject marker, a filename
marker, a bounded freshness window, an allowed extension, a minimal schema,
and Gmail `Authentication-Results` with aligned `dmarc=pass`.

Every accepted source attachment is SHA-256 bound to a receipt manifest. The
post-job verifier independently reopens the attachment and compares its digest.
It does not treat an email, filename, configured browser credential, or local
file path as proof that the source is fresh.

Supported scheduled-email source keys are:

| Source key | Report input | Minimum schema to configure after a real export is inspected |
|---|---|---|
| `tasks` | EZLynx task CSV | owner/assignee and status; normally `Assigned To`, `Status` |
| `activities` | EZLynx activity CSV | author and activity type; normally `Created By`, `Activity Type` |
| `sales` | EZLynx Sales Center CSV | opportunity, account, producer, stage, last activity |
| `retention` | EZLynx Retention Center CSV | account, owner, expiration, last activity |
| `submissions` | EZLynx Submission Center CSV | submission, account, owner, created date, status |
| `magellan_json` | prepared Magellan JSON | `source_status`, `records`, `by_employee` |

Do not guess the real export headers or sender addresses. Capture one sanitized
sample from each vendor, then copy only its verified column names and sender into
the Test manifest. Use each source's `modes` list to limit it to `daily`,
`weekly`, and/or `monthly` jobs. Real customer exports and phone numbers must not
enter Git.

## Individual Gmail checks

Employee mailboxes are audited with `gmail.metadata` only. The collector does
not request or persist message bodies or subjects. `ACCOUNTABILITY_GMAIL_USERS`
is the explicit mailbox allowlist; the initial Test value is Jackie and Jazmin.

For each recent thread, only the last message's sender and timestamp are used:

- External customer sender: the employee owes the next reply.
- The employee or an explicitly configured mailbox alias: the customer owes the
  next reply.
- Another `streetsmart.insurance` sender: excluded as internal mail.
- `Auto-Submitted`, bulk/list precedence, no-reply, mailer-daemon, or postmaster:
  excluded as automated mail.
- Missing or invalid sender metadata: excluded as ambiguous.

The output contains a salted-by-mailbox evidence hash, timestamps, aging, and
aggregate exclusion counts. It does not contain the Gmail thread ID, subject, or
body. Shared inboxes and aliases must be explicitly configured; otherwise their
results are not suitable for employee conclusions.

Workspace domain-wide delegation must contain all three scopes before the
connection checker reports Gmail ready:

- `gmail.metadata` — employee mailbox aging
- `gmail.readonly` — scheduled report attachments and delivery read-back
- `gmail.send` — report delivery only, still disabled during Test validation

## Test setup and gate

1. Copy `deploy/accountability/connection-manifest.example.json` to the Test
   host's protected accountability configuration path.
2. Keep `delivery.enabled` false.
3. Set the delegated service-account identity and the Jackie/Jazmin mailbox
   allowlist in the Test environment. Store no private key; use keyless IAM
   signing and Workspace delegation.
4. Add verified Gmail scopes to `collection.gmail.confirmed_scopes` only after
   an admin has confirmed the exact OAuth client entry.
5. Configure the current RingCentral users, queues, and membership. Do not use
   old queue numbers as evidence.
6. Configure each EZLynx/Magellan attachment source from a sanitized real sample.
7. Run `python scripts/check_accountability_connections.py --manifest <path>`.
   Configured collectors without observed evidence remain not ready.
8. Run three new Test jobs with unique job IDs. For every run, verify the report
   artifact digest, source receipt manifest, attachment digests, and persisted
   `post_job_audit` verdict independently.
9. Keep any incomplete run `UNVERIFIED`; do not enable recipients or promote.
10. After all three pass, record the immutable Test artifact digest and request
    Carlo's approval for that exact digest. Production promotion must reuse it
    without rebuilding.

The remaining external inputs are the real EZLynx and Magellan export samples,
verified sender addresses/subject labels, Workspace OAuth client/scopes, current
RingCentral queue registry, approved recipients, and the exact Google Chat space.
