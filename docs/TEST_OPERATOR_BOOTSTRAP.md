# Inspection-only bootstrap approval bundle

Local review preparation only. No publication, issue/comment creation, GitHub
settings changes, SSH registration, helper installation or runtime action has
occurred. The local commit and hashes are supplied in the accompanying validation
manifest; preserve reviewed baseline `4ccfa6763e585c5a51fb31c243fda83fa19a9a4f`.
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
   and main branch, approved merge SHA/tree and actual pinned checkout SHA/tree,
   exact runtime pair, fixed human actor,
   current write/admin permission, unedited body, nonce and 30-minute expiry.
   Record the successful proof run ID and uploaded request/controller evidence.
5. Set the proof run variable and enable bootstrap. Submit a fresh
   `bootstrap-inspect` request. The authorization job verifies the proof run's
   workflow path, SHA, branch, issue_comment event, actor, first attempt and the
   actual successful validator job/step, and execution-path identity at the proof event commit. A skipped job or merely green unrelated
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
| `ROBIE_TEST_OPERATOR_SETUP_COMMIT` | Actual controller PR merge SHA, after reviewed-tree verification; must equal the fixed PR merge SHA and checkout HEAD; event main may advance |
| `ROBIE_TEST_OPERATOR_CONTROLLER_TREE` | Reviewed candidate Git tree SHA from the final manifest; must equal merge tree and checkout tree |
| `ROBIE_TEST_OPERATOR_PR_NUMBER` | Actual returned number of that controller PR |
| `ROBIE_TEST_OPERATOR_PR_ID` | Actual immutable numeric ID of that controller PR, from fresh GitHub metadata |
| `ROBIE_TEST_OPERATOR_EVENT_PROOF_ENABLED` | `EVENT_PROOF_V1` for proof only, then disable |
| `ROBIE_TEST_OPERATOR_EVENT_PROOF_RUN_ID` | Actual successful same-approved-controller proof run ID (the event main SHA may be newer) |
| `ROBIE_TEST_OPERATOR_BOOTSTRAP_ENABLED` | `BOOTSTRAP_INSPECT_V1` only for approved setup, then disable |
| `ROBIE_TEST_OPERATOR_ENABLED` | `INSPECT_ONLY_V1` only after successful bootstrap |
| `ROBIE_TEST_STOPPED_OPERATOR_ENABLED` | Remains unset/disabled |

`OPERATOR_ACTOR_IDS` is the literal `320188404` in all four workflows;
`ROBIE_TEST_OPERATOR_ACTOR_IDS` is no longer a consumed repository variable.
The obsolete `ROBIE_TEST_OPERATOR_ISSUE` is not used. Every workflow uses
`issue_comment: types: [created]`, requires PR conversation context and a protected-main event, and checks out only
`${{ vars.ROBIE_TEST_OPERATOR_SETUP_COMMIT }}`. Before checkout, fixed inline code
validates 40-hex commit/tree values and verifies the commit tree via GitHub; empty
values and branch names cannot fall back to main. No repository code runs before
this check. All authorization
jobs use contents/issues/pull-requests read permissions; bootstrap proof lookup
also needs actions read. Only separately gated execution jobs request OIDC.
Never use `pull_request_target`, a PR-head checkout, arbitrary issue code or a
fallback target. The target is authorized by ID/number, never by PR title.

Unrelated protected-main advances remain available without changing approval.
The pinned validator compares Git blob SHA, object type and mode for the fixed
execution manifest against the event commit, the actual executing workflow commit
(`GITHUB_WORKFLOW_SHA`), and freshly resolved current main. It also requires the
actual workflow ref to match the selected operator mode. Missing/duplicate paths,
truncated trees, lookup errors and any relevant difference refuse execution.
Checks repeat after queue/environment waits, before cloud authentication. The
proof verifier applies the same manifest comparison to an older proof event, so
unrelated changes between proof and bootstrap are allowed. Evidence records the
approved, event, executing-workflow, current-main and proof-event revisions.

The manifest lives inside the pinned validator and covers these fixed paths:

- `.github/workflows/test-operator-event-proof.yml`
- `.github/workflows/test-operator-bootstrap.yml`
- `.github/workflows/test-operator-inspect.yml`
- `.github/workflows/test-operator-stopped.yml`
- `scripts/test_operator_request.py`
- `scripts/test_operator_setup_proof.py`
- `scripts/test_operator_bootstrap.py`
- `scripts/test_operator_inspect.py`
- `scripts/test_operator_stopped.py`
- `scripts/ensure-gcloud-ssh-key.sh`
- `deploy/test-stopped-approval.disabled.json`

All executable local dependencies of these workflows are covered by tests.
External Actions/runner/GitHub infrastructure retain their existing trust; this
manifest is not a new isolation boundary. Changing only application code or docs
outside the manifest does not change the controller that is checked out.

**Security boundary:** byte comparison is meaningful drift detection under the
existing protected-main review policy. It cannot prevent an authorized main
change from replacing a workflow and removing all checks, or another trusted
workflow from exercising the principal's existing broad access. Existing WIF
already trusts this repository's main, and existing sudo is broad. No stronger
cloud-side workflow/commit enforcement or new IAM is claimed. Current-main reads
are snapshots, not locks against later pushes. If unbypassable protection against
trusted main authors is required, this no-IAM-change scope cannot provide it.

**Controlled migration for relevant changes:** disable the operator execution
flags; review the new controller and dependency manifest through protected-main
checks; obtain explicit owner approval for the new exact tree/merged PR binding;
verify and configure the new approved commit/tree/PR number/ID; update any host
helper/config in a separately reviewed, evidence-preserving procedure; rerun the
credential-free proof and verify helper bytes before enabling access. The initial
bootstrap deliberately refuses overwrites; it is not a migration shortcut. Never
replace only the SHA to defeat a failed comparison or automatically retarget a PR.
Routine unrelated main advances require none of these changes.

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
> setup evidence. I approve continued use of this pinned controller across
> unrelated protected-main advances only under the manifest checks and existing
> trusted-main boundary described above. I approve the stated setup and inspection
> environment policies. Stopped operations remain disabled; this approval does
> not authorize runtime installation, service changes, backlog release, new IAM,
> or Production actions.
