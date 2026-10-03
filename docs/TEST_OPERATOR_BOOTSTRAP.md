# Inspection-only bootstrap approval bundle

Local review preparation only. No publication, issue/comment creation, GitHub
settings changes, SSH registration, helper installation or runtime action has
occurred. The local commit and hashes are supplied in the accompanying validation
manifest; preserve reviewed parents `5377150b280a05a47e1f4ac6996b304dcfe239e4` and
`706f5a9f27300b406a5afd1a2e2cbe8f2b7db7d5`.
Runtime remains `42e872f4c86fc4b4e37f859fc390f0b7c832f373`, retained TGZ SHA256
`876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064`.
This bootstrap neither transfers nor installs that runtime package.

## Concrete setup sequence

1. Approve and publish the immutable controller commit in the manifest through
   protected-main PR checks. No ordinary deployment is triggered by these issue
   workflows. Record the resulting main SHA and verify its tree equals the
   reviewed tree before using it as the exact setup SHA; changed trees require
   new review. Do not use a moving branch as the approval identifier.
2. Use that controller's OWN merged PR conversation as the single operations
   thread. No new ordinary issue is created. Record its returned PR number,
   immutable numeric PR ID and actual merge SHA. Before activation, compare the
   merge commit's entire Git tree with the reviewed candidate tree from the
   validation manifest. The owner configures these values AFTER publication;
   none is embedded in the candidate, so there is no self-hash/PR-number cycle.
   Approved actor is fixed in code to **320188404 (carlo504)**.
3. Configure the repository variables below. Initially enable only event proof.
   Create `Test-Operator-Setup` restricted to protected main, with owner
   `320188404` as required reviewer and self-review permitted explicitly for this
   one-time owner setup. Verify the repository plan supports these protections;
   do not silently downgrade them. Create `Test-Operator-Inspect` main-only,
   allowing routine inspection after the approved setup without another reviewer.
4. Through the actual connected GitHub comment route, create a fresh single-line
   event-proof request on that exact merged PR conversation. The proof workflow uses only the ordinary
   read-only GitHub workflow token: no cloud authentication, OIDC, environment,
   SSH or deployment concurrency. It validates the actual event/fresh comment,
   configured PR number/immutable ID, merged/closed state, same repository base
   and main branch, merge SHA, GitHub commit tree, actual checkout SHA/tree,
   exact runtime pair, fixed human actor,
   current write/admin permission, unedited body, nonce and 30-minute expiry.
   Record the successful proof run ID and uploaded request/controller evidence.
5. Set the proof run variable and enable bootstrap. Submit a fresh
   `bootstrap-inspect` request. The authorization job verifies the proof run's
   workflow path, SHA, branch, issue_comment event, actor, first attempt and the
   actual successful validator job/step. A skipped job or merely green unrelated
   run is refused. All checks repeat after the setup environment/concurrency wait
   and BEFORE cloud authentication. Approve the exact queued setup run in GitHub.
6. The existing WIF identity authenticates through IAP to hermes-test-01, requiring
   effective instance OS Login TRUE. Register a runner-owned OS Login key with
   TTL **1 hour**; use no metadata-key fallback. Render root Python stdin only
   from the exact reviewed checkout plus validated data. No issue text becomes
   executable code, shell arguments, filesystem paths or arbitrary configuration.
7. Root bootstrap creates only the fixed inspection helper, its inspection-only
   config and private setup state described below. It rejects existing helper
   or config files, checks trusted root-owned paths, claims setup exclusively,
   writes/fsyncs exclusively, and reads back the helper digest/config. Existing
   or partial setup requires review; no overwrite, adoption, cleanup or retry.
8. Disable bootstrap and event-proof enable variables after successful setup.
   Enable routine inspection, submit a fresh `inspect` request from a new cloud
   session with Mac offline, and retain its Actions/host evidence. Keep stopped
   operations disabled. Reconcile Ralph's latest actions from current observations
   before any later prepare-hold authorization.

## Variables and commands

| Variable | Approved value / source |
| --- | --- |
| `ROBIE_TEST_OPERATOR_SETUP_COMMIT` | Actual controller PR merge SHA, after reviewed-tree verification; must equal event SHA and checkout HEAD |
| `ROBIE_TEST_OPERATOR_CONTROLLER_TREE` | Reviewed candidate Git tree SHA from the final manifest; must equal merge tree and checkout tree |
| `ROBIE_TEST_OPERATOR_PR_NUMBER` | Actual returned number of that controller PR |
| `ROBIE_TEST_OPERATOR_PR_ID` | Actual immutable numeric ID of that controller PR, from fresh GitHub metadata |
| `ROBIE_TEST_OPERATOR_EVENT_PROOF_ENABLED` | `EVENT_PROOF_V1` for proof only, then disable |
| `ROBIE_TEST_OPERATOR_EVENT_PROOF_RUN_ID` | Actual successful same-commit proof run ID |
| `ROBIE_TEST_OPERATOR_BOOTSTRAP_ENABLED` | `BOOTSTRAP_INSPECT_V1` only for approved setup, then disable |
| `ROBIE_TEST_OPERATOR_ENABLED` | `INSPECT_ONLY_V1` only after successful bootstrap |
| `ROBIE_TEST_STOPPED_OPERATOR_ENABLED` | Remains unset/disabled |

`OPERATOR_ACTOR_IDS` is the literal `320188404` in all four workflows;
`ROBIE_TEST_OPERATOR_ACTOR_IDS` is no longer a consumed repository variable.
The obsolete `ROBIE_TEST_OPERATOR_ISSUE` is not used. Every workflow uses
`issue_comment: types: [created]`, requires PR conversation context and exact
approved main SHA, and checks out only `${{ github.sha }}`. All authorization
jobs use contents/issues/pull-requests read permissions; bootstrap proof lookup
also needs actions read. Only separately gated execution jobs request OIDC.
Never use `pull_request_target`, a PR-head checkout, arbitrary issue code or a
fallback target. The target is authorized by ID/number, never by PR title.

Any later main advance makes new requests fail closed. Updating only the setup
SHA cannot restore availability because it must still equal this fixed PR's
merge SHA. Restoring service requires a separately reviewed controller migration
and matching PR/config/helper binding; there is no automatic retargeting, SHA
loosening or old-workflow rerun. This availability limit is part of the proposal.

All three request operations use the existing strict grammar; placeholders are
not runnable and must be replaced with fresh values:

```
/robie-test event-proof commit=42e872f4c86fc4b4e37f859fc390f0b7c832f373 sha256=876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064 nonce=<32 lowercase hex> expires=<UTC within 30 minutes>
/robie-test bootstrap-inspect commit=42e872f4c86fc4b4e37f859fc390f0b7c832f373 sha256=876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064 nonce=<new 32 lowercase hex> expires=<fresh UTC within 30 minutes>
/robie-test inspect commit=42e872f4c86fc4b4e37f859fc390f0b7c832f373 sha256=876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064 nonce=<new 32 lowercase hex> expires=<fresh UTC within 30 minutes>
```

Setup writes: `/usr/local/libexec/robie-test-operator.py` root:root 0644;
`/etc/robie-test-operator.json` root:root 0600; and
`/var/lib/robie-test-operator/` root-owned 0700 containing:
`operator.lock`, `inspection-bootstrap.json` and
`inspection-bootstrap-complete.json` (0600). Subsequent inspection requests add
0600 `comment_id-<ID>` and `nonce-<nonce>` replay files. The root config binds the
PR number in its existing `issue` field and the immutable PR ID in `pr_id`. It may create `/usr/local/libexec` root:root 0755 if
absent. It does not install the stopped helper or root hold approval, modify
units/sudo/IAM/WIF/firewalls, acquire the browser/global driver, touch client data,
read a job database, or install/change the runtime. Existing broad sudo remains
broad. `stopped_operations_enabled_by_bootstrap=false` describes this operation,
not certification that no other operator has enabled another path.

## Inspection evidence and remaining facts

The six supplied auxiliary names are fixed in inspection and the still-disabled
hold template, pending live revalidation:

- `robie-verification-audit-4246.timer` and `.service`
- `robie-verification-manual-renewal-4247.timer` and `.service`
- `robie-verification-mortgagee-4372.timer` and `.service`

Inspection returns the ten guarded plus six auxiliary unit states and pointers;
selected effective stop properties (stop command presence only, never command
arguments) and their digest; and running cron executable identity/digest. Missing
properties are explicit. It examines only observed `*.lock` filenames in the
fixed Test `robie-job-engine/data` directory, recording device/inode and current
kernel lock owners without reading contents or acquiring locks. No environments,
command lines, database rows, browser profiles or client records are exported.

These observations support further review; they do not prove an aggregate worker
lock contract or that cron's handler preserves children. Both flags remain false.
A lock elsewhere is not guessed or silently replaced by the known browser lock.
Inactive cron has no observed running executable. Missing facts require bounded
follow-up review before stopped operations can be enabled. Actual handoff,
external/manual fences, shared-cron outage and natural-drain prerequisites remain
separate runtime authorization.

## Access limits and proposed owner approval wording

The cloud connector's `add_comment_to_issue` action explicitly accepts
`pr_number` and documents top-level PR conversation comments. This proposal uses
that documented target, not an ordinary issue. Actual connector authorship/event
delivery on the fixed merged PR still must be proven by the cloud-credential-free
workflow before bootstrap. Fresh PR lookup uses the documented GitHub pull-request
read endpoint with pull-requests read permission. No new API service is introduced.
Do not broaden to another PR or fall back to workflow reruns if proof fails.
No native dispatch, repository-variable/environment-management or pending-deployment
approval action is currently exposed here. Those one-time GitHub settings and
owner approval need GitHub UI or an already authorized supported admin route;
no token, new credential or Mac terminal is requested. If that route is absent,
report the exact missing action instead of building a bypass.

Proposed approval (replace the commit with the validation manifest's immutable ID):

> I approve publication of controller commit `<exact reviewed controller commit>`
> through protected-main review, use of that exact controller PR’s merged
> conversation as the sole operations thread, and the inspection-only setup for
> actor 320188404. After the credential-free connector-event proof succeeds for
> the verified reviewed main tree, I approve the bounded setup run using existing
> WIF/IAP/sudo access to hermes-test-01, including one-hour OS Login key registration
> and installation/read-back of only the fixed inspection helper/config/private
> setup evidence. I approve the stated GitHub setup and routine-inspection
> environment policies. Stopped operations remain disabled; this approval does
> not authorize runtime installation, service changes, backlog release, new IAM,
> or Production actions.
