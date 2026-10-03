# Disabled Test cloud deployment candidate

Local implementation only. No publication, settings, permissions, dispatch or host
changes have been applied. This is a deploy-only extension to the existing
`deploy-test.yml` workflow. It is not the old stopped-install request, certification,
Production promotion, a general shell, or an arbitrary workflow dispatcher.

## Scope and immutable identities

The existing PR740 inspector remains pinned and usable. None of its variables,
helpers, workflow files or host config change. The deployment controller uses its
own future merged PR conversation, merge SHA/tree, and five separate variables:

| Variable | Required value after separate approval |
| --- | --- |
| `ROBIE_TEST_CLOUD_DEPLOY_ENABLED` | Unset by default; `TEST_DEPLOY_V1` only after enablement approval |
| `ROBIE_TEST_CLOUD_DEPLOY_COMMIT` | Actual merge SHA of this controller's own reviewed PR |
| `ROBIE_TEST_CLOUD_DEPLOY_TREE` | Exact reviewed candidate tree, verified against that merge |
| `ROBIE_TEST_CLOUD_DEPLOY_PR` | That merged PR's number |
| `ROBIE_TEST_CLOUD_DEPLOY_PR_ID` | That merged PR's immutable numeric ID |

This is a separate binding, not a migration of the live inspector. The new paths
are outside the old inspector's security manifest. Relevant deployment-controller
changes require new reviewed pins; unrelated protected-main changes remain usable.
The pinned deployment manifest includes the dispatcher/receiver workflows, validator,
claim renderer, build/transfer/installer scripts and OS Login key helper.

Future command grammar, not an instruction to dispatch:

```
/robie-test deploy commit=<40 lowercase hex> sha256=<64 lowercase hex> prior=<40 lowercase hex> prior_sha256=<64 lowercase hex> nonce=<32 lowercase hex> expires=<YYYY-MM-DDTHH:MM:SSZ>
```

Only carlo504's immutable user ID320188404 with current write/maintain/admin permission
is accepted. The exact single-line, unedited comment on the fixed merged PR is the
human request. The expiry must be within 30 minutes after creation. Each real
request requires prior user authorization for this exact new release/digest,
rollback target/digest, gateway restart, coordinated Test window and permitted
runtime effects. The source code cannot verify that a chat conversation granted
approval or that all external operators have handed off; the parent must obtain
and record those facts before posting. A valid connector comment acts as Carlo.

## Smallest integration and preserved controls

The dispatcher has contents/issues/pull-requests read plus **actions:write**, only
in its fixed dispatch job. It has no OIDC permission. It POSTs only a main
`workflow_dispatch` to `deploy-test.yml`, with deploy confirmation and original
comment ID. It does not accept workflow names, refs, shell text, stopped actions,
certification evidence or Production options. There is no automatic POST retry;
ambiguous dispatch must be reconciled read-only.

The receiving workflow independently fetches the comment, current permission and
merged PR. It verifies protected main, first run attempt, pinned controller tree,
actual checkout, executing workflow identity and relevant-file byte/type/mode
agreement. It repeats fresh authorization after build and before installation.
The workflow-bot actor is never substituted for the original human author.

The requested release must equal the receiving run's GITHUB_SHA. Main moving
between dispatch and reception fails closed; no moving-main fallback. Main can
advance later only if protected controller files remain unchanged. The existing
build is retained and its TGZ digest must equal the preapproved request before
cloud authentication. This is exact-byte matching of the deterministic archive,
not an arbitrary historical-release downloader. Existing archive verification,
Test target checks, active-job inventory, skip-policy-setup, rollback, live proof,
installed-byte artifact retention and deploy concurrency remain intact.

The normal manual workflow path remains available under its existing authority;
this adapter is not a new security boundary around every repository workflow.
Unchanged Production provenance still requires a successful main workflow_dispatch
Test run whose source equals the release commit. No guard is weakened to accept
new provenance. Cloud-comment requests to certify/install-stopped are refused.

## Replay state and host effects requiring approval

Cloud execution uses the existing Test WIF/IAP/sudo identity, requires instance
OS Login TRUE, and registers a one-hour key using the existing helper. No IAM,
public access, credential storage, service-account grant or driver acquisition is
added. Existing Test workflow SSH/SCP registrations are also explicitly bounded to
one hour; the manual transfer key path remains supported. Existing authority is broad; actions:write is repository-scoped, not an IAM
restriction to just this workflow. The implementation bounds its use in code.

Before staging, a fixed root Python program rendered from the pinned controller
checks the actual Test hostname, both rollback pointers and the prior digest file.
It writes only this new replay state:

- `/var/lib/robie-test-cloud-deploy/`: root-owned0700;
- `lock`, `comment_id-<ID>`, `nonce-<nonce>`: root-owned0600.

Claims use exclusive no-follow creates, fsync and a host lock. The immutable JSON
binds the original request, controller and receiving run ID. Partial claims stay
consumed. Another run, reused nonce/comment, symlink, changed prior digest/pointers,
expired request or changed receipt refuses. A second check immediately precedes the
existing installer. There is no automatic cleanup, receipt replacement or retry.
Credential exchange/key registration happen before the claim: replay protection
prevents duplicate staging/install through this route, not repeated authentication.
The lock serializes claims; it does not fence outside operators or business workers.
Deployment concurrency and real operator coordination remain necessary.

The existing deploy operation itself changes Test release/runtime configuration
and restarts its gateway. Those effects need a per-release authorization; the
inspection-only approval does not cover them. Evidence includes request/controller
JSON, claim receipts and the existing installation proof. Root replay state is
retained independently of GitHub artifact retention. Even failure requires
reconciliation before a fresh authorized request.

## Enablement sequence and unresolved boundary

1. Review this diff and tests independently, publish a disabled PR only when
   authorized, pass CI, and obtain approval for its exact controller tree and the
   actions:write dispatch job plus root replay directory. Merge normally.
2. Verify its merge tree and own PR ID; configure the four immutable bindings.
   Keep the enable flag unset until the separate security approval is recorded.
   Do not repoint PR740's inspection variables or enable stopped/Production flags.
3. Explicitly approve the deployment approval model. This candidate does not
   introduce a GitHub required-reviewer gate. The existing private-plan limitation
   still applies: human authorization is chat plus controlled comment dispatch,
   not independently enforced GitHub human review. Admin/trusted-main bypass
   remains within the existing trust boundary.
4. Obtain a fresh exact release digest before posting, verify current rollback
   identity/digest and coordinated Test window; enable and submit one request.
   Observe the receiver's actual run and artifacts; do not equate dispatch receipt
   with installation success. Disable if the approved scope/window ends.

**Certification is deliberately unchanged.** Its current main-SHA coupling means
certification must still occur with that same main source. Supporting certification
after unrelated main advances needs a separately reviewed release/controller
provenance binding and authenticated independent reviewer evidence; merely accepting
a bot-supplied reviewer or dropping SHA checks is unsafe. That change affects the
contract consumed by Production and is not silently included here. Production
promotion and old stopped controls remain disabled in this cloud command path.
