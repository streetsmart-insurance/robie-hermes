# ROBIE release process

Status: bootstrap specification. This process is not yet the accepted Production deployment path.

## Environments

- **Test:** isolated GCP configuration, Job database, evidence, schedules, secrets, service identity, browser profile, Chat/testing ingress, and release pointer.
- **Production:** existing always-on StreetSmart runtime. No direct source edits.

Both profiles may be managed from one restricted Antigravity workspace, but they must never share writable state or deployment credentials.

## Release contract

`feature branch -> tests and security checks -> immutable artifact -> Test deploy -> end-to-end verification -> stored evidence -> approval -> promote same digest -> Production verification`

Every release record must include:

- commit SHA and branch;
- immutable release digest;
- Test and Production target identifiers;
- test/security results;
- deployment actor and scoped identity;
- verification evidence and recording links;
- approval identity and timestamp;
- previous verified Production digest; and
- rollback verification result.

## Initial bounded proof

The first Antigravity-managed release must be a low-risk, reversible change that does not alter EZLynx action behavior, Job state transitions, credentials, IAM, browser persistence, or message ingestion. A plain-English reporting/ledger presentation change is preferred.

Acceptance requires a feature branch, passing checks, Test deployment, stored end-to-end evidence, explicit approval, immutable Production promotion, post-promotion verification, and successful rollback rehearsal. Until every item passes, Cloud Shell remains the emergency/bootstrap deployment route and Antigravity is not the standard Production interface.

