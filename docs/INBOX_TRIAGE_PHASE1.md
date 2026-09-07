# Phase 1 inbox triage — Test implementation and integration handoff

**Hello update:** Carlo's later separate handoff moves human-confirmed
Producer/CSR routing into Phase 1 for Hello. See [HELLO_INTAKE_HANDOFF.md](HELLO_INTAKE_HANDOFF.md)
for the superseding Hello contract; Phase 2 still owns automatic classification.
The original explicit-assignee plan below continues to apply to the other flows.

This candidate contains three separate Python builds and their shared intake
contracts. It is not connected to the live EZLynx API, not registered in the
production Job Engine, and not an installed inbox automation. No live task has
been created. Synthetic tests are not destination evidence.

## What each phase does and why

Phase 1 isolates the original email or carrier document, matches it to exactly
one EZLynx account/policy, and prepares an assigned task for independent API
read-back. Isolating first gives retries a stable identity and preserves the
original evidence. Matching first prevents putting client work on the wrong
account. The time savings come from removing manual Gmail-to-EZLynx re-entry.
An intake receipt is not completed service or an issued certificate.

The unverified Documents API availability is handled as a manual-upload obligation in every
task. The original bytes are retained with a checksum in the configured private
artifact store; the task links to the source and identifies the original file.
A human uploads and verifies it, then performs the requested service. Task
verification confirms the intake handoff only; it cannot confirm the upload.

Phase 2 adds SOP classification, labels, workflow routing and discussion notes
after Phase 1 is stable. Phase 3 installs autonomous server scheduling after
Test verification and the required release approval. Neither is enabled here.

## Three separate builds

| Build | Entry point | Current behavior | Unfinished live connection |
| --- | --- | --- | --- |
| Certificates inbox | `CertificatesIntake.run_selected` | Reads a selected Gmail message, preserves original MIME bytes and attachments, matches, prepares assigned certificate-review task | Gmail read-only delegated identity, approved inbox, live EZLynx port |
| Hello inbox | `HelloIntake.run_selected` | Reads a selected Gmail message, preserves it, matches, prepares assigned general-review task | Gmail read-only delegated identity, approved inbox, live EZLynx port |
| Progressive retrieval | `ProgressiveRetrieval.retrieve_selected`, then `perform` | Bounds date window and chosen document; verifies scope, actionability, prior delivery and unticked service items before accepting download | Tested read-only Progressive portal adapter with dedicated credentials |

Progressive scopes remain separate: FAO Communications by processed date,
Policies Need Service unticked items, and BOP/Contractor GL Pending Cancel for
Nonpayment. Dates are explicit and inclusive, including weekends and holidays.
The implementation does not advance retrieval checkpoints or mark carrier items
processed. Until the portal adapter exists, `retrieve_selected` operates only
against injected fixtures.

## Verified discovery

- Repository inspection found `EZLynxTaskEngine.generate_task_payload`, which
  builds callback text but does not call an API. `EzlynxApiAdapter` in the poller
  is a protocol with a mock, not the live task-creation integration Carlo described.
- The connected GitHub organization returned only `robie-hermes` to this session.
  The live service's location and task API contract therefore remain unknown.
- Jake's existing browser session was observed signed in as `jferrara3` on
  September 7, 2026. Browser login is not proof of server API connectivity.
  No passwords/cookies were copied or persisted and no user settings changed.
- Jake's reply authorized using his login for this work. It did not identify
  numerical assignment IDs or long-term service credentials. Assignee IDs must
  be supplied and freshly resolved to exactly one active user before creation.
- The repository's compiled EZLynx write allowlist remains in force. Its current
  ID is used only by synthetic fixtures here; no live test against it occurred.

## Live EZLynx adapter contract — required from Carlo/integration owner

Jake supplied the [EZLynx Postman collection](https://documenter.getpostman.com/view/56716523/2sBYApyCjN).
`ezlynx_intake_reader.py` now implements Test-only, exact-ID reads from its
documented contracts: `Applicant/v2/{id}`, `Policy/{id}` with unencrypted IDs,
`User/{id}`, and `User/Users/{orgId}`. It verifies policy/account membership and
normalizes active user records. It requires an explicit HTTPS API base URL and
the existing server's credential provider; redirects are refused and credential
values are never embedded. No live connection has been exercised.

The collection uses an API-host placeholder and does not document task search,
creation, independent task read-back, or general applicant search. Its applicant
item named Search is an ID lookup. The reader deliberately holds unsupported
operations. It cannot turn an unidentified email into an assigned live task yet.

The collection also lists `POST /document` and Document Library list/download
operations. This establishes a documented interface, not agency entitlement or
successful upload. Keep the manual-upload obligation until availability and
destination verification have been demonstrated in Test.

`EzlynxIntakePort` is an internal boundary. It does not assert that EZLynx uses
these names or supports these endpoints. Map it to the existing server adapter
only after its actual API documentation and response samples are inspected.

1. `lookup_candidates(Identifiers)`: fresh, fully paginated account/policy
   results with applicant ID, policy ID/number, effective date, and any requested
   name/contact fields. Identity hints are operator-supplied in this first cut;
   the email sender is not assumed to be the insured. Name-only, no-match,
   ambiguous policy terms and contradictory identifiers require human review.
2. `lookup_assignee(user_id)`: one exact active user. No name guessing, producer
   fallback or automatic department routing. Confirm assignees and due-time
   rules independently for Certificates, Hello and Progressive.
3. `find_source_tasks(source_key)`: authoritative lookup across existing tasks
   by stable source identity. Persist source key/digest, source URL and the
   manual-upload obligation in fields that can be read back from EZLynx.
4. `find_related_work(applicant_id, policy_id, source)`: check relevant existing
   activities/documents/workflows. Existing related work holds for human attachment
   or update; it does not create a parallel workflow. If Documents API absence
   prevents a reliable prior-delivery check, return incomplete and obtain human
   confirmation rather than falsely reporting no duplicate.
5. `create_task_once(task)`: at-most-one task per source key, including concurrent
   requests and timeouts after the remote write. A lookup-then-POST alone is not
   sufficient. Prove server/native idempotency or durable serialization and
   reconciliation before enabling this method; do not guess a custom HTTP header.
6. `read_task(task_id)`: a fresh API read normalized to the task fields. A task
   receipt alone is never completion proof. Wrong account, policy, assignee,
   due time, source data or missing manual-upload flag fails verification.

Missing/stale/incomplete reads must set `authoritative=False` or `complete=False`.
An empty complete result means proven absence, not an authentication failure.
Every hold retains the source artifact reference for a human review queue.

## Server and Job Engine wiring still required

These flow classes return `WorkerResult`; their explicit source/identifier
arguments are not yet the Job Engine's `perform(job, idempotency_key)` signature.
The existing integration owner must wire a durable job adapter that loads a
preserved source, invokes the correct separate flow, persists holds and receipts,
and registers `IntakeVerifier` for independent read-back. A crash or uncertain
write must reconcile by source key before any repeat write. Do not call these
classes directly from a production timer.

Raw source content stays in a private artifact directory outside the repository,
with 0600 files and a 0700 directory. Choose server storage, backup/retention,
and a staff-accessible retrieval path before collecting real items. Existing
artifact mismatch is held for recovery, never overwritten. Existing directories
with group/world permissions are refused. No raw customer samples
belong in Git. No labels, email replies, archive actions, issuance, policy
changes, or portal completion clicks are implemented.

## Validation and release gate

Run the dependency-free checks with:

```sh
ROBIE_ENV=TEST python -m unittest discover -s tests -p '*intake*.py' -v
```

The repository's existing unittest-discovery regression battery picks them up.
The 36 focused tests pass locally, including 11 documented-reader tests for
field mappings, account membership, missing task APIs and transport safeguards.
A broader local run (excluding the existing
browser-fixture module) recorded 862 passed, 2 skipped and the same two macOS
versus Linux rollback-tool failures seen on the earlier baseline. The final two
intake tests and the 11 reader tests were added afterward and are included in the focused pass. The
broader run is not represented as a full-suite pass; GitHub CI remains required.
They cover all three flows, ambiguous and contradictory matches, original-file
preservation, repeated and cross-flow source replay, a timeout after a remote
write, incomplete API results, existing related work, wrong read-back, active
assignee checks, the compiled applicant scope, Production refusal, and bounded
Progressive retrieval. Fixture idempotency is not proof of live API idempotency.

Before Phase 1 acceptance, run all three separate flows through the real adapter
on approved Test fixtures, including missing documents, duplicate delivery,
session expiry and a process restart after an uncertain write. Verify the task
and manual-upload instruction in EZLynx, test staff retrieval of the original,
and record saved Job Engine evidence. Follow the repository's new-job-type gate
and exact-digest release process. Test release/digest, rollback target and live
completion evidence are currently UNVERIFIED. Nothing in this draft certifies
readiness for production or completion of Phase 1.

## SOP sources and unresolved decisions

- [Certificates SOP](https://docs.google.com/document/d/18-rHfPe4pe0pUon_I3Vr9NpVFX3DNij_GvkhgcNslOU/edit): written requests, one-hour processing target and licensed coverage review. Discussion title and Automation Center remain TO CONFIRM in the source. Those placeholders are not invented here.
- [Hello / Mail Sorting SOP](https://docs.google.com/document/d/1ypTB9sf1TaSPA0F_hW8ODezvFQpQRln-BywiNwcGW40/edit): existing-work checks, unidentified items, and the three Progressive retrieval sections.
- Required next information: live integration repository/service, its Test
  connection and task lookup/create/read-back contract; three approved assignee
  IDs and due-time rules; authorized inboxes and source storage; dedicated
  Progressive Test login/adapter. Secrets go directly into Secret Manager.

Gmail requires `https://www.googleapis.com/auth/gmail.readonly` for the selected
raw-message read; the accountability module's metadata-only permission cannot
read original messages. Use the official [messages.get contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)
and a separately approved read-only service. Do not widen the accountability
collector's scope as a side effect of this build.
