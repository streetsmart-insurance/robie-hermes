# HERMES infrastructure runbook

End-to-end operator guide for **recovery**, **secret rotation**, and **deploy**
on StreetSmart Hermes POC infrastructure.

**Project:** `streetsmart-hermes-poc`  
**Zone:** `us-east1-b`  
**Production VM:** `hermes-poc-01` → `/opt/streetsmart-hermes`  
**Test VM:** `hermes-test-01` → `/opt/streetsmart-hermes-test`  
**Repository:** `streetsmart-insurance/robie-hermes` (protected `main`)

This document does not contain secret values. Never paste credentials into Chat,
the Job ledger, recordings, or this repository.

---

## 1. Environments and separation

| Profile | VM | Install root | Gateway unit | Service account |
| --- | --- | --- | --- | --- |
| **Production** | `hermes-poc-01` | `/opt/streetsmart-hermes` | `hermes-gateway` | `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| **Test** | `hermes-test-01` | `/opt/streetsmart-hermes-test` | `robie-gateway` | `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` |

Rules:

- Test is **not** a Production clone. Test must **not** read Production secrets.
- A Test all-clear is **never** a Production all-clear.
- Engineering builds on feature branches; Production promotion requires Carlo's
  explicit approval of the exact QA-certified digest.
- No service-account JSON keys. GitHub Actions uses Workload Identity Federation (WIF).

Full release contract: `RELEASE_PROCESS.md`.

---

## 2. Access (before any operation)

### 2.1 Human SSH (IAP + OS Login)

One-time bootstrap:

```sh
gcloud auth login your@email.com
gcloud config set project streetsmart-hermes-poc
bash scripts/ensure-gcloud-ssh-key.sh
```

Cloud Shell transcript proof: `bash scripts/cloud-shell-restart-ssh-proof.sh` — see
`docs/CLOUD_SHELL_SSH_BOOTSTRAP.md`. Legacy passphrase keys are auto-rotated.

Connect:

```sh
gcloud compute ssh hermes-test-01 \
  --zone=us-east1-b \
  --project=streetsmart-hermes-poc \
  --tunnel-through-iap

gcloud compute ssh hermes-poc-01 \
  --zone=us-east1-b \
  --project=streetsmart-hermes-poc \
  --tunnel-through-iap
```

- Keys live under `$HOME/.ssh/google_compute_engine` (no passphrase).
- SSH ingress on Hermes network: IAP range `35.235.240.0/20` only.
- Admin grant/revoke: `scripts/grant-team-ssh-access.sh` (instance `osLogin` + project `iap.tunnelResourceAccessor`).

Details: `docs/SSH_TEAM_ACCESS_RUNBOOK.md` (Task 1 branch / merged doc).

### 2.2 GitHub Actions identities (keyless)

| Purpose | WIF provider | Service account |
| --- | --- | --- |
| Test deploy + diagnostics | `projects/1036123102831/.../github-actions/providers/github` | `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com` |
| Production deploy | `projects/751771086524/.../github-production/providers/github-main` | `robie-production-deployer@streetsmart-hermes-poc.iam.gserviceaccount.com` |

Details: `docs/GCP_ACCESS_RUNBOOK.md`.

---

## 3. Deploy process — end to end

### 3.1 Release contract (summary)

```text
feature branch → CI green → immutable artifact → Test deploy → E2E evidence
→ Carlo approval → same digest to Production → post-deploy verify → evidence
```

Every release record must include: commit SHA, artifact SHA-256, Test/Production
targets, test results, deploy actor, verification evidence, approval identity,
previous Production digest, rollback verification.

### 3.2 Build immutable artifact (local or CI)

From a reviewed commit on a feature branch or `main`:

```sh
bash scripts/build-release.sh /tmp/releases
source /tmp/release.env
echo "commit=${release_commit} sha256=${release_sha256}"
bash scripts/verify-release.sh "${release_archive}" "${release_archive}.sha256"
```

`verify-release.sh` re-checks digest, compiles extracted source, and runs the
dependency-free acceptance suite **before** any host may consume the archive.

### 3.3 Deploy to Test (`hermes-test-01`)

**Preferred:** GitHub Actions workflow **Deploy immutable release to Test**

1. Merge to protected `main` with green CI.
2. Actions → **Deploy immutable release to Test** → Run workflow.
3. Confirmation input: `DEPLOY_TO_HERMES_TEST_01`.
4. Workflow builds artifact from `GITHUB_SHA`, authenticates via WIF Test deployer,
   SCPs archive + `scripts/deploy-test-release.sh` to staging on Test VM, runs installer.

**Manual (emergency bootstrap only):**

```sh
# From workstation with IAP SSH
gcloud compute scp /tmp/releases/robie-hermes-<short>.tgz \
  /tmp/releases/robie-hermes-<short>.tgz.sha256 \
  scripts/deploy-test-release.sh \
  hermes-test-01:/var/tmp/robie-test-deploy/<commit>/ \
  --zone=us-east1-b --project=streetsmart-hermes-poc --tunnel-through-iap

gcloud compute ssh hermes-test-01 ... --command='
  sudo bash /var/tmp/robie-test-deploy/<commit>/deploy-test-release.sh \
    --archive /var/tmp/robie-test-deploy/<commit>/robie-hermes-<short>.tgz \
    --checksum /var/tmp/robie-test-deploy/<commit>/robie-hermes-<short>.tgz.sha256 \
    --commit <full-40-char-sha>
'
```

Installer refuses any host except `hermes-test-01`, verifies checksum, inventories
open RUNNING jobs, flips atomic pointers under `/opt/streetsmart-hermes-test`, restarts
`robie-gateway`, writes deployment evidence.

**After Test deploy:**

- Run bounded E2E / job-type gates per `RELEASE_PROCESS.md`.
- Store evidence (commit, digest, job IDs, recordings). Report `TEST VERIFIED` only
  with authoritative proof.

### 3.4 Promote to Production (`hermes-poc-01`)

**Requires Carlo's explicit approval** of the exact 40-char commit and 64-char SHA-256.

GitHub Actions workflow **Deploy immutable release to Production**:

1. Inputs: `commit` (40 hex), `sha256` (64 hex), `confirmation` = `DEPLOY_TO_HERMES_POC_01`.
2. Runs only on protected `main` + GitHub `Production` environment.
3. Checks out controller at workflow SHA and release at exact `commit` (no substitution).
4. Rebuilds and verifies artifact matches requested digest.
5. WIF → `robie-production-deployer@...`, IAP SSH to `hermes-poc-01`.
6. Runs `scripts/deploy-production-release.sh` with `--digest` matching approved SHA-256.

**On-host Production install (supported path):**

`scripts/install-official-release.sh` is the only supported Production pointer flip.
It installs Chat-critical overlays from the zip and refuses `done` until live proof:
pointers match, gateway `ActiveEnterTimestamp` after flip, Chat dests equal zip,
`install_proof` row exists.

```sh
# After gateway restart per environment approval gate
sudo bash scripts/install-official-release.sh prove \
  --release-root /opt/streetsmart-hermes/releases/<short> \
  --opt-root /opt/streetsmart-hermes \
  --sha <commit> \
  --gateway-unit hermes-gateway
```

**After Production deploy:**

- Regression battery runs from gateway `ExecStartPost` (isolated workdir).
- Run production preflight / health checks.
- Persist evidence JSON from `/opt/streetsmart-hermes/deployments/<short>/`.
- Report `PRODUCTION VERIFIED` only after authoritative checks pass.

### 3.5 Application rollback (code only — not disk restore)

Before every deploy, installers capture rollback pointers (`current`, `releases/current`).

**Test rollback** (automatic on failed verify inside installer, or manual):

Pointers restored via `scripts/lib/test-release-rollback.sh` → `rollback_test_release`
then `systemctl restart robie-gateway`.

**Production rollback:**

1. Identify previous verified digest from deployment evidence / `deployments/`.
2. Confirm open jobs and leases — do not delete Job DB or artifacts.
3. Restore previous release pointers to the prior `releases/<short>` tree.
4. Restart `hermes-gateway` only.
5. Run health checks + bounded verification.
6. Record rollback evidence.

Workflow summary: `.agents/workflows/rollback.md`.

---

## 4. Secret rotation

### 4.1 EZLynx login (`ezlynx-username`, `ezlynx-password`)

**Where Production reads secrets:**

- Host: `hermes-poc-01`
- Project: `streetsmart-hermes-poc`
- Bootstrap: `ezlynx_login_bootstrap.py` → resolves **newest ENABLED** version (not `versions/latest`)

**Rotation procedure (Pawel / authorized operator):**

1. Add a **new ENABLED** Secret Manager version (never echo password to terminal log):

   ```sh
   bash skills/ezlynx-session-login/scripts/provision-secrets.sh streetsmart-hermes-poc
   ```

   Or add versions manually with `gcloud secrets versions add` (no stdout of payload).

2. **Do not** destroy the old ENABLED version until the new one is verified.
3. DESTROYED versions cannot be restored. A DESTROYED `latest` with an older ENABLED
   version is healthy — bootstrap uses ENABLED v1, not dead `latest`.
4. If a job HITL'd on login: reply **RETRY** in the Robie Chat HITL thread after rotation.
5. Verify states only (no payloads):

   ```sh
   PYTHONPATH=. python -m robie_job_engine.login_secret_health
   ```

**Automatic guard:** `robie_job_engine/login_secret_health.py` — preflight + scheduler;
alerts when no ENABLED version exists.

Full on-call text: `LOGIN_SECRETS.md`.

### 4.2 Other secrets (pattern)

| Secret | Typical accessor | Rotation |
| --- | --- | --- |
| `hermes-api-token` | `hermes-poc@` SA only | Add new ENABLED version; verify gateway; revoke old after proof |
| `api-server-key` | `hermes-poc@` SA only | Same |
| `robie-google-oauth-token` | Production runtime only | Add version; restart Chat bridge if needed |
| Test-only secrets | Test SA only (if any) | Never grant Test SA on Production secret names |

**Rules:**

- Grant `roles/secretmanager.secretAccessor` per-secret, not project-wide Editor.
- Test VM must be denied Production secrets (`scripts/prove-test-production-secret-denial.sh`).
- Never paste secret payloads into Chat, jobs.db, or recordings.

### 4.3 Secret Manager version discipline

Runtime resolves newest **ENABLED** version. Pinned or `versions/latest` on a
DESTROYED version causes login HITL. See `robie_job_engine/secret_resolution.py`.

---

## 5. Recovery

### 5.1 Automated disk snapshots (Production boot disk)

| Setting | Value |
| --- | --- |
| Policy | `daily-hermes-poc-snapshot` |
| Schedule | Daily **04:00 UTC** |
| Retention | **14 days** |
| Disk | `hermes-poc-01` boot (75 GB) |

Bootstrap / verify schedule:

```sh
bash scripts/configure-hermes-poc-snapshot-schedule.sh
```

Manual snapshot before risky change:

```sh
gcloud compute disks snapshot hermes-poc-01 \
  --project=streetsmart-hermes-poc \
  --zone=us-east1-b \
  --snapshot-names=hermes-poc-manual-$(date -u +%Y%m%d)
```

### 5.2 Non-destructive restore drill

Proves snapshot integrity **without** modifying Production:

```sh
bash scripts/prove-hermes-poc-snapshot-restore.sh <snapshot-name>
```

Creates drill disk from snapshot → mounts on `hermes-test-01` → verifies
`/opt/streetsmart-hermes` → deletes drill disk.

Details: `docs/HERMES_POC_BACKUP_RECOVERY.md`.

### 5.3 Full VM recovery (destructive — Carlo approval required)

Use when boot disk or VM is lost/corrupted and application rollback is insufficient.

1. **Stop traffic:** pause gateway scheduler; notify operators.
2. **Pick snapshot:**

   ```sh
   gcloud compute snapshots list \
     --project=streetsmart-hermes-poc \
     --filter='sourceDisk~hermes-poc-01'
   ```

3. **Stop instance:**

   ```sh
   gcloud compute instances stop hermes-poc-01 \
     --project=streetsmart-hermes-poc --zone=us-east1-b
   ```

4. **Replace boot disk from snapshot** (adjust names):

   ```sh
   gcloud compute disks delete hermes-poc-01 --zone=us-east1-b --quiet
   gcloud compute disks create hermes-poc-01 \
     --zone=us-east1-b \
     --source-snapshot=<snapshot-name> \
     --type=pd-standard
   gcloud compute instances attach-disk hermes-poc-01 \
     --disk=hermes-poc-01 --boot --zone=us-east1-b
   gcloud compute instances start hermes-poc-01 \
     --project=streetsmart-hermes-poc --zone=us-east1-b
   ```

5. **Verify:** IAP SSH → `systemctl is-active hermes-gateway` → production preflight →
   bounded Chat health.
6. **Record:** snapshot name, time, verification output in deployment evidence.

Application code rollback (section 3.5) may still be needed if snapshot predates
the desired release.

### 5.4 Access recovery

| Symptom | Action |
| --- | --- |
| `Permission denied (publickey)` | Re-run `ensure-gcloud-ssh-key.sh`; verify instance `osLogin` + project `iap.tunnelResourceAccessor` |
| IAP tunnel fails | Check `hermes-allow-iap-ssh` firewall; VM tag `hermes-poc` |
| WIF deploy fails | Verify GitHub environment, WIF provider condition (main-only), SA bindings |
| OS Login external user | Org may require `roles/compute.osLoginExternalUser` |

### 5.5 Service recovery (no redeploy)

```sh
# Production
sudo systemctl status hermes-gateway
sudo systemctl restart hermes-gateway
sudo systemctl status robie-ezlynx-browser   # if EZLynx automation stuck

# Test
sudo systemctl status robie-gateway
sudo systemctl restart robie-gateway
```

Production preflight (infra health, not job authorization):

```sh
PYTHONPATH=/opt/streetsmart-hermes/current \
  python3 -m robie_job_engine.production_preflight
```

---

## 6. Monitoring and alerts (infra layer)

Separate from application job health:

| Alert | Source | Bootstrap |
| --- | --- | --- |
| VM down / disk full / gateway crash | Cloud Monitoring | `scripts/configure-hermes-infra-alerts.sh` |
| Cost spike | Billing budget | `scripts/configure-hermes-billing-budget.sh` |
| Login secret states | `login_secret_health` | Scheduler + Chat alert |
| Production host health | `production_preflight` | systemd timer |

Details: `docs/HERMES_INFRA_MONITORING.md`, `docs/HERMES_BILLING_ALERTS.md`.

---

## 7. IAM least privilege

- Audit export: `scripts/audit-hermes-iam.sh`
- Hardening: `scripts/harden-hermes-iam.sh`
- Expected minimum: `deploy/iam/expected-minimum-access.json`

Details: `docs/HERMES_IAM_REVIEW.md`.

---

## 8. Evidence checklist (every infra change)

| Field | Required |
| --- | --- |
| Commit SHA | 40-char |
| Artifact SHA-256 | 64-char |
| Environment + VM | `hermes-test-01` or `hermes-poc-01` |
| Actor / identity | human email or WIF SA |
| Before/after pointers | `readlink -f /opt/.../current` |
| Verification output | preflight, battery, or drill transcript |
| Approval | Carlo timestamp for Production |
| Rollback target | previous verified digest |

---

## 9. Quick reference — scripts

| Script | Purpose |
| --- | --- |
| `scripts/build-release.sh` | Build immutable `.tgz` + SHA-256 |
| `scripts/verify-release.sh` | Pre-deploy verify gate |
| `scripts/deploy-test-release.sh` | Test VM installer |
| `scripts/deploy-production-release.sh` | Production VM installer (GitHub path) |
| `scripts/install-official-release.sh` | Supported Production pointer flip + prove |
| `scripts/configure-hermes-poc-snapshot-schedule.sh` | Snapshot policy |
| `scripts/prove-hermes-poc-snapshot-restore.sh` | Non-destructive restore drill |
| `skills/ezlynx-session-login/scripts/provision-secrets.sh` | EZLynx secret rotation |
| `scripts/prove-test-production-secret-denial.sh` | Verify Test secret isolation |
| `scripts/ensure-gcloud-ssh-key.sh` | Non-interactive SSH bootstrap |
| `scripts/grant-team-ssh-access.sh` | Admin: grant human SSH |

---

## 10. Related documents

| Document | Topic |
| --- | --- |
| `RELEASE_PROCESS.md` | Full release gates and job-type promotion |
| `LOGIN_SECRETS.md` | EZLynx login secret on-call |
| `docs/GCP_ACCESS_RUNBOOK.md` | WIF / keyless GitHub access |
| `docs/HERMES_POC_BACKUP_RECOVERY.md` | Backup schedule + restore drill |
| `docs/SSH_TEAM_ACCESS_RUNBOOK.md` | IAP + OS Login team access |
| `docs/HERMES_IAM_REVIEW.md` | IAM audit |
| `docs/HERMES_INFRA_MONITORING.md` | Infra alerts |
| `AGENTS.md` | Engineering operating contract |
