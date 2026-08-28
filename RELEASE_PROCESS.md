# ROBIE release process

Status: required Production gate. This is the accepted path, not a suggestion.

## Environments

- **Test (`hermes-test-01`):** isolated GCP configuration, Job database, evidence, schedules, secrets, service identity, browser profile, Chat/testing ingress, and release pointer.
- **Production (`hermes-poc-01`):** existing always-on StreetSmart runtime. No direct source edits.

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

`scripts/build-release.sh` creates the archive directly from a reviewed Git
commit, rejects sensitive/runtime paths, and emits its SHA-256. Test and
Production must consume that exact archive and checksum. `scripts/verify-release.sh`
revalidates the digest, compiles the extracted source, and runs the complete
dependency-free acceptance suite before a deployment can proceed.

Production zip install is `scripts/install-official-release.sh`: flip both
pointers and install every Chat-loaded overlay from that zip, then refuse
`done` until dest equals the zip (bytes or zip-load shim). Pointer-only is
not live. The script does not `git pull`, bind, print secrets, or overwrite
user-owned Loom `ascend-finance`. Skills stay a separate Drive → `.hermes`
install. See CURRENT_STATE.md.

## Required gate: new job types / LOB skills

Carlo's standing rule: **before any new job type** (personal auto, homeowners,
or any new LOB/skill) goes near Production (`hermes-poc-01`), it must run
**3 clean jobs on Test (`hermes-test-01`) that PASS the automated post-job
audit**, then promote. This is a required gate, not a suggestion.

N = **3**. A clean job means the persisted `post_job_audit` checkpoint has
`verdict=PASS` (heartbeat present, destination evidence nonzero when the
contract requires it, recording shows motion, no tool-vs-recording MISMATCH).
The audit never authorizes `COMPLETE`; destination verification remains the
only COMPLETE authority.

### How the gate is enforced (no SSH)

1. **Skill flag.** New LOB skills declare `job_type` and `production_ready: false`
   in SKILL.md frontmatter until the Test record exists.
2. **In-repo promotion file.** After 3 passing Test audits, write
   `deploy/job_type_gate/promotions/<job_type>.json` with:

   ```sh
   python3 scripts/check-job-type-gate.py record-from-db \
     --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db \
     --job-type ezlynx.personal_auto
   ```

   That script reads Test `jobs.db` and writes the record into the repo. Carlo
   does not SSH to Production.
3. **CI.** `.github/workflows/ci.yml` runs
   `python3 scripts/check-job-type-gate.py check`. A new skill marked
   `production_ready: true` without 3 Test PASS audits fails the build.
4. **Runtime.** On `ROBIE_ENV=PRODUCTION`, `bounded_schema_hold_reason` refuses
   a new (non-grandfathered) job type until the promotion record exists.

**Grandfathered:** commercial auto (`ezlynx-commercial-auto-from-quote` /
`ezlynx.commercial_auto`) and the already-live bounded Job Engine types. Those
Production paths stay open. The gate applies to NEW types going forward.

## Production pre-flight is infra only

`robie_job_engine/production_preflight.py` watches Production host health
(gateway, CDP, EZLynx tab, login-secret versions, conversation binds, Chat
intake / Pub/Sub listener, Chat-runtime dests equal the zip). It is **infra only**. A pre-flight yes is not a
job-type gate and does not replace Test.

New job types still need **N clean Test (`hermes-test-01`) jobs** before
Production on a real account. N = **3**. See the required gate above.

## Automated post-job audit

On every Chat/Job Engine terminal state (`COMPLETE`, `FAILED`, `UNVERIFIED`),
the Job Engine runs a four-answer audit and the Robie Chat APP posts it into
the **same job thread**. It does not mark the job COMPLETE. It does not bind,
pay, or write EZLynx. Check 3 (recording motion) and check 4 (tool vs
recording) treat a recorder attached to a different tab than the Playwright
page as **frozen / MISMATCH**. Job 30777947's first-ezlynx-wins CDP attach
stayed on a stale Policies listing while playwright_exec drove documents /
Policy Edit / FormEntry. Check 3 stays fail-closed: a frozen recording is
FAIL even if tool logs are busy. The recorder change is not done until a
TEST recording shows real motion (non-identical frames) while Playwright
navigates the driven page. Carlo does not need to SSH to see that.

## Initial bounded proof

The first Antigravity-managed release must be a low-risk, reversible change that does not alter EZLynx action behavior, Job state transitions, credentials, IAM, browser persistence, or message ingestion. A plain-English reporting/ledger presentation change is preferred.

Acceptance requires a feature branch, passing checks, Test deployment, stored end-to-end evidence, explicit approval, immutable Production promotion, post-promotion verification, and successful rollback rehearsal. Until every item passes, Cloud Shell remains the emergency/bootstrap deployment route and Antigravity is not the standard Production interface.
