# Local Test stopped-operator proposal

Review-only controller work. Not published, enabled, installed or live-tested.
Read with TEST_STOPPED_INSTALL.md and TEST_OPERATOR_INSPECT_PROPOSAL.md. The
original approved runtime package is unchanged: commit
`42e872f4c86fc4b4e37f859fc390f0b7c832f373`, TGZ SHA256
`876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064`.
No ordinary deployment, resume, backlog release, Production or client write is
added. Controller changes are not a replacement runtime package.

## Implemented command path

The strict existing issue grammar accepts `hold`, `install`, or `verify` only
when `ROBIE_TEST_STOPPED_OPERATOR_ENABLED=STOPPED_OPERATOR_V1`. Inspection retains
its separate enable variable. Every request binds the original commit/digest,
actor, designated issue, fresh nonce and 30-minute expiry. Fresh GitHub permission
checks run before and after the protected execution queue. Shared concurrency
starts only after authorization. No issue text is shell code or outage approval.

The credentialed job uses `Test-Operator-Stopped`, intended to require an
independent reviewer for mutation access, unlike routine bounded inspection.
It invokes only this fixed command:

```
sudo -n /usr/bin/python3 -I -B /usr/local/libexec/robie-test-stopped-operator.py operate
```

Validated request JSON arrives on stdin. The root helper independently requires
an existing root-owned 0600 `/etc/robie-test-stopped-approval.json`, binding its
own installed SHA256, exact package, allowed operations/actor/issue and a window
of at most 24 hours. Approval needs actual outage, fencing, drain and operator
handoff references. It must explicitly include all Test cron work, manual/direct
producers, worker drain, exclusive handoff, and no resume. The supplied disabled
JSON intentionally contains null evidence and false attestations. It cannot run.
Never change these to true just to satisfy the helper.

`hold` requires the gateway AND every existing service, including cron, already
inactive/dead with no worker PID/cgroup processes. Active timers may be stopped
after original state capture. This deliberately does not implement a safe drain
for a running gateway or cron: that requires the existing verified reservation,
actual intake fence and approved outage procedure. An idle DB snapshot alone
never substitutes. Unknown/missing schemas, active or expired unresolved runs,
leases and reply sends fail closed through the exact approved package guard.

Before masks: capture the gateway's real ExecStart Python/venv identity; original
unit states and local unit file identity; exact release pointers; durable-row
fingerprints; old release tree, policy skill, browser service and driver metadata
fingerprints. Save an exclusive fsynced attempt journal. Rename replaced local
unit definitions into `/etc/systemd/system/.robie-stopped-42e872f4c86f/`, preserving
inode, link, ownership, mode and content; never remove preexisting masks. Apply
only persistent `/dev/null` masks to the approved unit list, reload, verify idle
state and unchanged DB, then exclusively write/fsync the genuine hold receipt.
Missing original interpreter for an already-masked gateway requires review.

`install` validates receipt, masks, backups, preserved state and original queues.
It exclusively claims the install attempt and executes installer bytes read
directly from the exact original TGZ, with `--skip-policy-setup --keep-stopped`.
No rebuild occurs. It inherits the host lock into the installer subprocess, so
parent death cannot release the lock while that installer still runs. Existing
release/snapshot/claim or failed installation refuses automatic retry. Existing
installer rollback remains stopped; the wrapper never unmasks or repairs jobs.

`verify` independently checks the receipt/masks and no service workers, archived
source bytes and exact generated overlays, current pointers, captured interpreter,
runtime drop-in, dependency imports from the release-specific runtime directory,
all saved durable rows, original release tree/unit/drop-in backups, and unchanged
browser service, fixed global-driver metadata attribute and policy skill. It
records runtime tree SHA256 after successful install and compares it on later
checks. It reports TEST INSTALLED STOPPED, never live or independent business QA.
It never contacts client APIs or reads browser profiles. Metadata access is only
the fixed non-secret `robie-ezlynx-driver` attribute, not token/secret endpoints.
Installer output and database fingerprints stay root-private on Test; Actions
receives only bounded status/boolean/hash evidence.

## Existing access facts supplied by Mac read-only inventory

Observed 2026-10-02 23:54 UTC; no security changes were made:

- WIF provider `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github`
  ACTIVE; condition owner ID `320189022`, repo ID `1343750842`, `refs/heads/main`.
  Existing SA WIF binding uses `attribute.repository/streetsmart-insurance/robie-hermes`.
- Deployer `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com`,
  unique ID `101626854851847726954`; OS Login `sa_101626854851847726954`,
  UID/GID `4225702345`; effective sudo `(ALL) NOPASSWD: ALL`.
- Existing instance AND project `compute.osAdminLogin`, plus `compute.viewer`.
  Existing IAP condition restricts instance `6971056864475829887`, port 22,
  project `751771086524`, zone `us-east1-b` (hermes-test-01).
- Attached runtime SA `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com`;
  deployer already has serviceAccountUser. No new IAM is needed for the functional route.
- GitHub `carlo504`, ID `320188404`, admin. Default workflow token read; protected
  main enforces strict `test` and applies to admins. Existing `Test` environment
  has no protections. It is not an approval gate for the proposed operator.

These facts prove broad existing capability, NOT effective command restriction.
The helper's allowed operations do not reduce that principal's sudo rights.
Existing broad access does not authorize every action. Tightening or replacing
that identity is a separate security project, not a hidden setup dependency or
permission grant in this proposal. No new IAM, sudo/WIF grant, storage.admin,
public access, runtime-SA expansion or credentials are proposed.

## Exact setup bundle to review before approval

The controller is not published. Its immutable local commit and helper hashes
are supplied in the handoff/validation record after local review (a document
cannot contain its own future commit ID). Do not approve an unknown commit or
a moving main. The proposed actions, once those identifiers are recorded:

1. Publish/review/merge that exact controller change through normal protected-main
   checks, without deploying it as the runtime. Create one dedicated ordinary
   issue titled `ROBIE Test operator requests`. Record its returned numeric ID;
   do not invent it. Approve actor allowlist containing only `320188404` initially.
2. Create `Test-Operator-Inspect` with main-only access and no per-inspection
   reviewer after initial access proof. Create `Test-Operator-Stopped` main-only
   with an explicitly selected independent reviewer and self-review prevented.
   Reviewer identity and supported environment protection options remain unresolved.
3. Through the already authorized Mac/IAP route, install reviewed helper files
   root-owned and non-writable by the login user, preserving any prior files:
   `/usr/local/libexec/robie-test-operator.py` (inspect) and
   `/usr/local/libexec/robie-test-stopped-operator.py` (stopped operations).
   Existing sudo already suffices; adding a helper rule would not restrict it.
4. Create root:root 0700 `/var/lib/robie-test-operator/` and
   `approved/42e872f4c86fc4b4e37f859fc390f0b7c832f373/` below it. Provision the
   retained exact TGZ there root:root 0600 and independently compare its digest.
   Do not precreate installer/checksum files: helper creates those exclusively.
   No GCS bucket or additional storage permission is required.
5. Create root-private inspect config with the actual issue/actor/release values.
   Install the stopped approval JSON DISABLED, with false fencing/drain fields.
   Creating this disabled file does not attest an outage. Record prior files,
   hashes and rollback before changes.
6. Set repository issue/actor variables and enable inspection only after actual
   connector comment delivery/actor and workflow permission lookup are proven.
   Temporary OS Login registration is explicitly part of approved execution and
   has TTL 1h; no durable private key is placed in this cloud workspace.
7. Enable the stopped workflow only after separate explicit approval for that
   persistent trigger. Before mutation, an authorized operator must supply the
   actual completed fence/drain/handoff evidence, shared-cron outage authorization,
   observed old pointer, helper hash and validity window in the root approval.
   Stop if Carlo has not approved cron or other operators have not relinquished
   direct activity. Issue commands alone cannot supply or amend that approval.
8. Prove inspect from a fresh cloud session with Mac offline, then separately
   run approved hold/install/verify on the same TGZ and record run IDs/evidence.
   Retain the root approved package and before/after evidence; never delete queues.

## Explicit limits and unresolved decisions

- No native dispatch tool is callable here. Authenticated read identity is known,
  but actual connector comment authorship/event delivery is unproven. No comments
  or issues have been created. Test that route after approval; do not weaken actor
  checks if the connector emits bot comments or an event is suppressed.
- Shared cron outage and actual external/manual fencing approval remain missing.
  This helper cannot stop an active cron/gateway/service to guess safe drain.
- A single actual operator must own Test before mutation. Shared host lock covers
  these new helpers and inherited installer only; existing Mac/manual and ordinary
  deployment entrypoints do not honor it. Existing Actions deployments share the
  concurrency group, but an attestation cannot physically fence arbitrary root
  shells. Handoff and direct producer fencing must be real.
- A Mac-created receipt/installed target or an interrupted helper attempt is not
  automatically adopted or overwritten. Reconcile its actual evidence manually.
- Approval bytes are bound to the attempt. Extending expiry or changing approval
  after hold causes follow-up refusal. Complete within the approved window or
  obtain a separately reviewed evidence-preserving recovery procedure; never
  rewrite evidence/replay records or unmask merely to pass checks.
- This is not a general-purpose deployment platform or a running-gateway drain
  controller. Hold/install support is deliberately this approved release only.
  Other releases need a separately reviewed immutable approval mechanism.

Rollback of access setup disables enable variables first and restores only newly
changed infrastructure from recorded backups. It never removes runtime masks,
starts services, or releases backlog. Partial runtime recovery follows the exact
approved stopped-install runbook with preserved evidence and separate authority.
