# Chat/reply backport and exact-package promotion

This candidate starts at main `8e63df2c684abf7ace040ba776093934ab18dffe`.
It ports the generation fix `177e955cea7f9d1486e5d023e494ed712becb4dd`
and the reply, thread, receipt, context and session prerequisites from PR726.
It does not merge PR723. Phone/Bland features, renewal workers, new document
filing services, new Playground live-port wiring, and SOP retrieval changes
are excluded. Requester/thread approval checks and existing write guards remain.

## Candidate review and runtime coordination

Review the exact source commit and changed-path manifest before publication.
The existing combined branch and PR726 remain recovery references, not the
source of an implicit broader merge. No current Test or Production version is
inferred from a local checkout or a successful CI workflow.

Before Test, independently inventory loaded source/digest, both release pointers,
active jobs, browser sessions and the supported shared-driver lease. Preserve the
installed package, rollback pointers, current skills/runtime configuration and
durable jobs. Refuse an occupied driver lease or active job; do not override it.
Only Test applicant `26356199` may be considered for separately authorized work;
this release's QA contract instead requires zero client writes and filing disabled.
The Chat deployment workflows explicitly pass --skip-policy-setup on both hosts.
They preserve the existing skill on install and rollback; Test evidence must say
policy_setup_changed:false. The legacy general installer mode still validates and
installs a homeowners-only Test skill for applicant220250093, but that mode is not
used by this Chat release and is not authorization for any client action.

## Promotion operations

All credential-bearing dispatches remain on protected `main`. Existing WIF
providers, service accounts, VM guards and Production environment approval stay
unchanged. No new IAM or credential setup is part of this candidate.

1. Dispatch `deploy-test.yml` on reviewed main with operation `deploy` and
   confirmation `DEPLOY_TO_HERMES_TEST_01`. Its installer refuses active jobs,
   preserves rollback and independently verifies the loaded version. It retains
   `installed-test-release-<commit>` containing the exact TGZ, checksum and
   Test installation evidence for 30 days. This is not QA certification.
2. Conduct independent, bounded Test QA and record authoritative results for
   the exact source and TGZ digest. Do not treat parity INCONCLUSIVE as PASS.
3. Dispatch the same workflow with operation `certify`, confirmation
   `CERTIFY_TEST_PACKAGE`, original Test run/artifact IDs, release SHA-256, and
   the independent QA JSON below. This separate job has only Actions/Contents
   read permissions, no GCP identity and no deploy/build step. It verifies the
   original Test provenance and bytes, binds the reviewer to the authenticated
   workflow actor, and retains `verified-test-release-<commit>`.
4. Dispatch Production only after the separate release review. Supply exact
   `commit`, `sha256`, certification `test_run_id`, certification
   `test_artifact_id`, and `DEPLOY_TO_HERMES_POC_01`. Production validates run
   provenance, both artifact ZIP and TGZ digests, all evidence and allowed
   bundle members, then uses the retained TGZ. It never rebuilds the release.

Certification requires the same protected-main SHA as the original Test run.
If main advances before certification, stop and review; do not relabel evidence
or substitute the newer source. Expired/missing packages fail closed. Re-running
certification does not redeploy Test. Do not use workflow reruns to bypass a failed
QA or deployment gate.

The reviewer submits this schema only after obtaining the corresponding evidence;
these fields are requirements, not default answers:

```json
{
  "commit": "<exact 40-character source SHA>",
  "release_sha256": "<exact 64-character TGZ SHA-256>",
  "environment": "Test",
  "host": "hermes-test-01",
  "reviewer": "<authenticated GitHub actor who independently reviewed QA>",
  "verified_at": "<UTC timestamp after installation>",
  "installed_source": {"run_id": "<original Test run ID>", "artifact_id": "<original installed artifact ID>"},
  "passed": true,
  "applicant_ids": ["26356199"],
  "filing_enabled": false,
  "client_writes_performed": false,
  "driver_lease_clear": true,
  "check_evidence": {
    "<each of the eight check names below>": {"uri": "<durable https or gs evidence URI>", "sha256": "<64-character evidence digest>"}
  },
  "checks": {
    "generation_restart": "PASS",
    "stale_receipt": "PASS",
    "concurrent_turn": "PASS",
    "reply_recovery": "PASS",
    "service_account": "PASS",
    "secrets": "PASS",
    "browser": "PASS",
    "job_db": "PASS"
  }
}
```

Each check requires its own durable evidence URI and SHA-256. The certification
workflow binds installed_source to the successfully downloaded original package.
QA timestamps must follow installation. These checks validate provenance structure,
not the truth of referenced evidence; independent human review remains required.
Keep supporting evidence with the review. Secret readiness must be established
without copying secret payloads. Neither this JSON nor an installation proof
authorizes a client write. Immediately before Production, repeat health, job/lease
and rollback inventory; an earlier Test attestation cannot clear a current conflict.

`verify-release.sh` runs the runtime-only verifier. It does not replace the full
CI battery, scoped generation/reply tests, or independent Test evidence.

## Rollback

Use the exact independently recorded prior artifact and pointers, not current main.
Preserve generation checkpoints and pending reply-outbox records. The old code
cannot safely interpret string generation tokens as its former integer counter.
Before reverting, coordinate process shutdown and review unfinished generations;
never replay uncertain writes or erase job state to make rollback appear clean.
No live rollback has been rehearsed by this local candidate validation.

## Backport review corrections

Receipt review tightened two imported prerequisite behaviors: metadata-only count
changes no longer confirm an unidentified note, and a POST exception retains the
sent-unconfirmed marker. An uncertain send does not expire into automatic retry.
A matching returned note ID or matching text in fresh destination evidence remains
required. Explicit repost approval is separate from receipt authority. These guards
have synthetic regression coverage; no note POST was performed for this review.

The Chat generation/recovery and note-receipt pytest modules are explicit CI and
Test-workflow gates, because unittest discovery alone does not collect them.

## Dependency choices and exclusions

This is a dependency-complete backport, not a six-file patch. Main does not have
turn_finalization, the reply outbox, thread binding, job controls, client lookup,
note deduplication ledger, or user-facing reply formatter. The gateway and guards
need those interfaces together. Cancellation/session release, recorder cleanup,
question-only routing, API receipt correlation, and requester-bound clarification
are coupled prerequisites. The independent email formatter change and login navigation/reload changes have
been removed. Named lookup no longer installs a live CDP searcher: absent an injected
authorized searcher, Chat asks for an explicit applicant ID without touching the browser. Existing Playground live writer wiring stays
on the main implementation; no new playground_ports module is included.

Python runtime requirements are unchanged. The promotion validator uses only the
standard library plus the runner's existing gh command. Test dispatch installs
PyYAML, already declared by this repository and used in CI, for workflow tests.
No phone/Bland modules, renewal worker implementation, new document-filing service,
PFA implementation, IAM binding, credential file, or live configuration is added.
The integration-test path change uses the existing durable test helper rather
than a writable home directory. The process cleanup test uses an isolated Linux
child subreaper to verify SIGKILL and reap orphaned fixture children; production
process cleanup behavior is not weakened for this runner.

The note ledger now serializes check/reservation/POST/readback using a bounded
interprocess lock and fsyncs updates. Repost approval consumption is atomic.
Terminal cleanup cannot release another running turn's shared session lease.
Concurrent external writers still cannot make metadata-only counts authoritative.
The installer job inventory is not a lock on new claims: supported driver ownership
and coordinated intake quiescence through flip/restart remain deployment gates.

## Source provenance correction

Public PR726 remains at 7d4b0d8d6367c5b2c933c3d44db7d85cae0954c7.
The preserved 5e97bb1eb6907563d05fa8e4f33c8486bf1494b9 is an unpublished local
follow-up; it must not be described as the public PR head.

## Independent review deployment gates

Two independent review passes found and addressed note-ledger concurrency, one-use
approval races, shared-session cleanup, live named-lookup CDP bypass, and incomplete
QA provenance. They did not certify the deployed runtime or waive required checks.
Before using skip-policy mode, verify the existing policy skill is an independent
directory or points to an immutable target. A link through current/releases/current
would change indirectly when the release pointer flips, even without relinking it.
No such runtime inspection was performed here.

Skip mode still provisions the existing release-local gateway Python dependency
bundle and PYTHONPATH drop-in on Test, flips the supported release pointers, and
restarts the gateway. Those are real deployment effects, not policy writes.
The installer inventories jobs once but does not hold an admission/driver lock
through installation. Current supported driver ownership, coordinated intake
quiescence and a verified rollback artifact are required before any dispatch.
