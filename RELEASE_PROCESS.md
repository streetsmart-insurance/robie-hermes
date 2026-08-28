# ROBIE release process

Status: required Production gate. This is the accepted path, not a suggestion.

## Environments

- **Test (`hermes-test-01`):** isolated GCP configuration, Job database, evidence, schedules, secrets, service identity, browser profile, Chat/testing ingress, and release pointer.
- **Production (`hermes-poc-01`):** existing always-on StreetSmart runtime. No direct source edits.

Both profiles may be managed from one restricted Antigravity workspace, but they must never share writable state or deployment credentials.

**Test is not a Production clone.** `hermes-test-01` has different
service-account permissions and must not read Production secrets. A Test
all-clear is never a Production all-clear. Known diffs live in
`deploy/regression_battery/parity.json`. Update that list when a deploy
or HITL shows drift (new permission, secret, browser, or ingress gap).
A check that cannot be proven on Test because of a documented gap is
**INCONCLUSIVE**, never green.

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

Production zip install is `scripts/install-official-release.sh` — the only
supported Production flip. Do not type a long SSH cookbook. The script
flips both pointers and installs every Chat-critical file (adapter,
Playwright tools, write-guard, gemini helper) from that zip, then refuses
`done` until live proof holds: pointers match, Chat dests equal the zip,
`hermes-gateway` `ActiveEnterTimestamp` is after the flip, and an
`install_proof` row exists. Pointer-only is not live. Chat looking busy is
not live. COMPLETE still requires destination-verified evidence > 0. The
script does not `git pull`, bind, print secrets, restart Chrome, or
overwrite user-owned Loom `ascend-finance`. Skills stay a separate Drive →
`.hermes` install. See CURRENT_STATE.md.

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

## Required gate: NEW site / workflow (Ascend, next carrier portal)

Commercial auto already on Production is **not** a free pass for a NEW site
or workflow. Before Ascend, the next carrier portal, or any other new
browser workflow may run on a real Production account, it must pass this
Test-only Job Engine job on `hermes-test-01`:

- Job type: `ascend.locator_artifact_audit`
- Battery scenario: `ascend:locator-and-artifact-audit`
- Named Production path bug: `artifact-path:concat-job-id-eb96f620`
  (`job_id[:-11]+artifact_id` lookup is FAIL; folder must be the full job id)
- HITL Chat tone: `hitl-tone:dry-playwright-blocked` (cowboy/slang rewritten
  to dry `PLAYWRIGHT_BLOCKED` + path + ask before send)
- Sender roles: `ascend-roles:sender-not-robie-ai` (Producer / Account
  Manager from `requested_by`; never Robie AI when Jake or Carlo sent)
- Programs spinner: `ascend-new-program:wait-spinner` (unique `+ New program`,
  never the caret; timeout is `PLAYWRIGHT_BLOCKED`)
- Skill: `ascend-locator-artifact-audit` (`production_ready: false`)
- N = **3** clean Test jobs still applies

The job is a **full Job Engine job**, not a UI click-through script. It
walks the Ascend new-program flow with Playwright strict mode (unique
locator required; non-unique is FAIL; no Gemini; no `.first` / `.nth` /
`.last`) and saves / looks up a quote PDF the same way a live Chat job
does (artifacts under the real job id). It fails if the worker cannot
open the PDF it just saved (mangled / concatenated job-id folder). It
stops before Save program, Send email, Copy checkout, payment, or bind.
Test account only. Never PAWIVA / 221398001 / a real client.

GitHub-hosted CI must not talk to live Ascend or EZLynx. CI unit-tests
the punch-list reporter and the artifact-path assertion. The live walk
is Test-VM only:

```sh
ROBIE_ENV=TEST python3 -m robie_job_engine.ascend_locator_audit --live \
  --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db \
  --artifact-root /opt/streetsmart-hermes-test/robie-job-engine/data/artifacts
```

This job ends as a punch-list report, not COMPLETE of a finance
agreement. COMPLETE is never allowed without destination evidence. This
tree must not flip Production. Loom `ascend-finance` is never overwritten.

## Production pre-flight is infra only

`robie_job_engine/production_preflight.py` watches Production host health
(gateway, CDP, EZLynx tab, login-secret versions, conversation binds, Chat
intake / Pub/Sub listener, Chat-runtime dests equal the zip). It is **infra only**. A pre-flight yes is not a
job-type gate and does not replace Test.

New job types still need **N clean Test (`hermes-test-01`) jobs** before
Production on a real account. N = **3**. See the required gate above.

## Automated regression battery

What runs automatically — do not reconstruct this from Chat.

| Where | What | Trigger |
| --- | --- | --- |
| GitHub Actions job `test` (ROBIE verification gate, with `canonical-paths`) | Logic suite: job-type gate, `unittest discover -s tests`, Phase 3 pytest modules, plus the in-process Test replay / known-failure catalog (`quote_replay` missing-PDF + Production-path refuses). No EZLynx. No `hermes-test-01`. | Every `pull_request` and every push to `main` |
| `hermes-poc-01` after zip pointer flip + `hermes-gateway` restart | Same battery (`python -m robie_job_engine.regression_battery --notify --detach`) from `deploy/systemd/zz-hermes-gateway-job-engine-path.conf` `ExecStartPost`. Leading `-` so a NEW fail does not take the gateway down. `--detach` so the suite does not block ready. Isolated workdir `/var/lib/robie-regression-battery` — never Production `jobs.db`. | Every Production deploy (gateway restart after pointer flip) |
| `scripts/verify-release.sh` | Same `--ci` battery after digest + compile, before a host may consume the archive | Pre-deploy verify |

This battery **only guarantees previously seen failures have not come
back**. Simulator passed means nothing we have already seen is wrong —
known scenarios did not regress — never that nothing is wrong. A Test
all-clear is not a Production all-clear.

**Same-day scenario rule:** every Production incident that was a NEW
failure mode gets a named deterministic scenario in
`deploy/regression_battery/scenarios.json` before we call the incident
closed. That is how the simulator grows. Add the scenario the same day.
Do not mark the incident closed until the id exists in the battery.

A **NEW** fail (a logic failure, or a replay outcome that is not in the
known-accepted catalog) posts to `spaces/AAQAZbLJO78` as the Robie Chat
APP via `chat_app_post.post_as_chat_app`. Known-accepted Test failures
stay quiet. DESTROYED Secret Manager latest with an older ENABLED
version is HEALTHY and does not alert. INCONCLUSIVE parity gaps may be
noted in Chat once and are never green. HITL resume-orphan and
false-success (COMPLETE-shaped prose, 0 destination evidence) are named
scenarios; Chat names the class if either returns. It does **not**
`@robie` (that would start a live job). It does not bind, pay, or email
the insured. There is no duplicate draft PR for the same signature.

A draft-PR hook may run (`gh pr create --draft`) only for a
deterministic NEW fail with a stable scenario id + the same failure
signature. Flaky / timeout / network blips do not open a PR. If an
unmerged auto-draft for that signature already exists, comment on it
instead of opening another. Each auto-draft names owners Carlo Ferrara
(StreetSmart) and Jake (StreetSmartJake) and says: Jake Approves, Carlo
Confirms, Dusty pings if it sits.

**Human gate (no self-ship):** Jake Approve (`StreetSmartJake`) and Carlo
Confirm, same as other changes. The battery must not merge, must not flip
Production, and must not restart `hermes-gateway`.

Live Test replay of a real quote PDF stays on `hermes-test-01` with
`ROBIE_ENV=TEST` and `/opt/streetsmart-hermes-test/...` paths. GitHub
runners and the Production oneshot refuse Production env and live Hermes
job-db paths. New job types still need N=3 clean Test jobs before
Production on a real account.

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
