# Local proposal: bounded Test inspection trigger

Status: REVIEW ONLY. Not published, enabled, installed, or live-tested. This describes
the inspect-only component. The separate bounded hold/install proposal is
`TEST_STOPPED_OPERATOR_PROPOSAL.md`; neither component is enabled. The concrete Mac-independent setup is documented in TEST_OPERATOR_BOOTSTRAP.md. No command here resumes
services, masks units, changes browser/global driver, reads a job database, or
creates an outage receipt. Existing unit files, prior-state backups and receipts
are never touched. Inspection does NOT prove a drained or fenced host.

## Immutable release boundary

Runtime package remains commit `42e872f4c86fc4b4e37f859fc390f0b7c832f373`, SHA-256
`876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064`.
The local controller branch must not become a replacement runtime release.
Both validator and host helper reject other release identifiers. The validator also
binds the configured merged PR identity and approved main commit/tree; the host
config binds its PR number and immutable PR ID. Later general
release support needs a separately reviewed approval registry. The retained
archive under `/workspace/stopped-install-evidence-42e872f4c86f/` is unchanged.

## Proposed trigger and trust boundaries

The existing connector has comment tools but no native dispatch tool. The controller’s exact merged PR conversation accepts exactly this single-line
grammar (placeholders are NOT runnable):

```
/robie-test inspect commit=<40 lowercase hex> sha256=<64 lowercase hex> nonce=<32 lowercase hex> expires=<UTC YYYY-MM-DDTHH:MM:SSZ>
```

Only creation events qualify. The workflow rejects ordinary issues, other PRs, unmerged PRs, edited/stale
comments, other repositories, non-main refs, workflow reruns, unapproved
numeric actor IDs, bots and actors lacking current write/maintain/admin rights.
It GETs the comment and collaborator permission from GitHub twice: before and
after execution queue/environment waits. Expiry must fall within 30 minutes of creation.
Comment text never becomes shell code. Neither job writes comments or issues.

The execution job uses the immutable approved controller SHA, WIF and
IAP. It sends validated JSON through stdin to one fixed, preinstalled root-owned
helper. The helper uses isolated Python, a fixed unit list and fixed pointer
paths. No arbitrary path, program, shell, environment, operation or release
argument is accepted. Output includes bounded unit properties, Test release pointers, helper hash, and
fixed cron/lock prerequisite diagnostics described in TEST_OPERATOR_BOOTSTRAP.md. Workflow compares installed helper bytes to controller
bytes. Output explicitly refuses hold/installation certification.

The helper treats the authorized SSH principal as the transport authority. Its
JSON actor ID is NOT an independent GitHub signature. A compromised authorized
SSH principal could request inspections directly. Therefore local sudo policy,
WIF restrictions and protected workflow review remain essential.

## Replay and serialization

Only the inspection job, after successful authorization, enters GitHub concurrency
group `robie-hermes-test-deploy`. There is no workflow-level concurrency group:
unrelated/unauthorized comments must not displace pending deployments.
A root-private `/var/lib/robie-test-operator/operator.lock` is held during
inspection. Before reads, exclusive, fsynced `comment_id-*` and `nonce-*` ledger
files consume both identifiers. A crash or partial claim fails closed. Reusing
either is refused; retry requires a new approved comment and nonce. No automatic
ledger deletion or retention cleanup is included.

This host lock coordinates ONLY this helper. Existing installers and manual/Mac
operators do not honor it. It does not authorize concurrent runtime mutation or
replace operator handoff. Before adding hold/install, all mutation paths must
share a reviewed host lock and reconciliation procedure.

## Exact permission/configuration manifest for owner review

No item below has been changed by this proposal.

| Surface | Proposed configuration | Approval/verification required |
| --- | --- | --- |
| GitHub connector | Existing repo-scoped comment creation + read access | Current-user reads identify `carlo504`, ID `320188404`; actual comment author/event still needs proof; no token copied to workspace |
| Repository variable | `ROBIE_TEST_OPERATOR_ENABLED=INSPECT_ONLY_V1` | Set LAST, only after setup approval; absent/other value disables jobs |
| Repository variables | `ROBIE_TEST_OPERATOR_PR_NUMBER`, `ROBIE_TEST_OPERATOR_PR_ID` | Owner records the exact controller PR number and immutable ID after publication |
| Actor | Fixed `320188404` in the workflows and validator | No configurable alternate actor |
| Environment | `Test-Operator-Inspect` | Proposed routine inspect-only policy: main-only, no per-run human reviewer after initial owner-approved setup and access proof; verify plan supports branch protections. Hold/install must use a separate approval policy |
| GitHub token | authorization: contents read, issues read, pull-requests read; inspection adds id-token write | No Actions write needed for issue trigger; confirm collaborator-permission GET works with this token or fail closed |
| WIF | Existing provider `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github` | Inspect live trust first; approve exact repository/owner IDs, main and this workflow/environment claims, including issue_comment event; no broad trust |
| GCP identity | Existing `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com` | Existing IAP/sudo success is evidence of capability, NOT least privilege |
| IAP/OS Login | Existing Test-only IAP and OS Login, needed Compute metadata reads and attached-SA actAs | Read live bindings before any proposed diff; no grants are part of this patch |
| SSH key | Runner-created OS Login key, TTL 1h | Explicit enablement includes this bounded credential registration; require instance enable-oslogin TRUE; never fall back to metadata keys |
| Host helper | `/usr/local/libexec/robie-test-operator.py`, root-owned, non-writable by login user, trusted parent directories | Privileged owner installs reviewed `scripts/test_operator_inspect.py`; routine inspection never uploads/installs it; separate approved bootstrap does |
| Host config | `/etc/robie-test-operator.json`, root:root 0600 | Populate enabled, commit, sha256, numeric actor_ids array, PR number in issue and immutable pr_id; exact IDs as above |
| Host state | `/var/lib/robie-test-operator`, root:root 0700 | New ledger/lock only; preserve across sessions/reboots; no reset to retry |
| sudo | Exact command `/usr/bin/python3 -I -B /usr/local/libexec/robie-test-operator.py inspect`, NOPASSWD for verified OS Login principal | No wildcard args, arbitrary python/shell, SETENV or writable helper; validate proposed sudoers with visudo |

The observed principal already has broad sudo. This proposal does not narrow it
and requires no new IAM/sudo identity or grant. The authoritative concrete setup
sequence and exact approval wording are in TEST_OPERATOR_BOOTSTRAP.md; the bounded workflow below implements that sequence.
No Production identity, runtime-SA expansion, firewall/public access or secret
payload permission is added.

## One-time owner actions (not executed)

Follow TEST_OPERATOR_BOOTSTRAP.md: publish the exact reviewed tree through normal
protected-main checks, bind its own merged PR number/ID and merge SHA, prove the
actual documented PR-comment connector route without cloud credentials, and then
approve the bounded inspection-only bootstrap through the setup environment.
No new ordinary issue or sudo/IAM policy is required. Unrelated main advances are allowed only when the execution manifest is unchanged;
relevant changes require separate reviewed migration. A fresh cloud inspection
with the Mac offline must succeed before claiming usable access.

## Separate stopped-operation component

`TEST_STOPPED_OPERATOR_PROPOSAL.md` now describes the local hold/install/verify
component, its separate disabled trigger, mandatory root-owned outage approval,
and the bounded already-stopped-gateway preparation path. The Mac's fresh access inventory
in that document supersedes earlier unverified IAM/sudo statements in this initial
inspection manifest: functional access exists but includes unrestricted sudo.
No new IAM is needed. Existing broad rights do not authorize new actions.
Shared cron approval, real fencing/drain/handoff, the designated merged controller PR,
protected stopped-operation reviewer and trigger proof remain required.

Rollback for initial access: disable repository enable variable first, revoke
only newly approved helper access if needed, preserve replay/evidence files and
restore recorded infrastructure configuration through the owner. Never unmask
or start application services as part of access rollback.

## Local review corrections and remaining trigger gap

Independent review identified pre-authorization shared-concurrency interference
and failure on absent optional systemd units. Both are corrected locally with
regressions: shared concurrency is job-level after successful authorization;
`systemctl show --all` output accepts optional `not-found/inactive/dead` units
with no observed worker PID. Missing properties remain null, never fabricated
zeros. Missing primary gateway, contradictory states, duplicate/unknown fields
or unexpected output still refuse inspection.
Re-review also identified that loaded timers have no Service-interface PID
properties. Typed timer handling now preserves those unavailable fields as null;
service units still require their PID properties. Realistic loaded timer fixtures
and unexpected nonzero timer-PID refusal are covered.

Harmless connector `get_profile` and `get_user_login` calls both resolve
`carlo504` / `320188404`. This resolves the connected read identity, not proof of
comment actor/event delivery or an approved actor allowlist. No issue/comment
was created. A harmless collaborator-permission read returned `admin` for this
user on the repository. Available tools still contain no native workflow dispatch action;
rerun tools cannot supply new inputs or establish the hold. No known setting or
permission grant makes an absent tool callable. Native dispatch requires an
actually supported connector capability; otherwise this reviewed PR-conversation trigger
needs the end-to-end proof above. No persistent workspace credential is proposed.
