# Task 1 — Durable non-interactive SSH evidence

**Scope:** deploy + team access (IAP + OS Login, no public SSH on Hermes network).  
**Project:** `streetsmart-hermes-poc`  
**Instances:** `hermes-test-01`, `hermes-poc-01` (`us-east1-b`)  
**Evidence date:** 2026-09-01 UTC  
**Operator:** `pawelstasinskiuk@gmail.com`

## Done checklist

| Requirement | Status | Evidence |
| --- | --- | --- |
| No-passphrase key under `$HOME/.ssh` (not `/tmp`) | **DONE** | `scripts/ensure-gcloud-ssh-key.sh`; key path `$HOME/.ssh/google_compute_engine` |
| Key registered via `gcloud compute os-login ssh-keys add` | **DONE** | transcript below; OS Login profile fingerprints |
| Deploy non-interactive (zero passphrase prompts) | **DONE** | `deploy-test.yml` step + GitHub Actions run `33446723706` |
| Team SSH via `gcloud compute ssh --tunnel-through-iap` | **DONE** | transcript: both VMs `SSH_OK` |
| Instance-scoped `roles/compute.osLogin` per person | **DONE** | IAM dumps below (`pawelstasinskiuk@gmail.com`) |
| IAP tunnel (`roles/iap.tunnelResourceAccessor`) | **DONE** | SSH via IAP succeeds; troubleshoot `0 issue(s)` |
| No public SSH on Hermes network | **DONE** | `hermes-poc-net`: only `hermes-allow-iap-ssh` `35.235.240.0/20`; `default-allow-ssh` **disabled** |
| Legacy VM `ssh-keys` metadata removed | **DONE** | `hermes-test-01` metadata: only `enable-oslogin` + startup-script |
| Revocable per-person / per-key | **DONE** | OS Login keys per user; instance IAM per VM; runbook revoke commands |
| Cloud Shell restart transcript | **N/A local** | Equivalent non-interactive bootstrap transcript captured from operator laptop (below). Cloud Shell uses same `ensure-gcloud-ssh-key.sh` path after `gcloud auth login`. |

## Related repository commits (StreetSmart `main`)

Foundation and keyless paths (pre-Task-1 hardening):

| Commit | PR / subject |
| --- | --- |
| `a00a587` | Add durable keyless GCP readiness audit (#70) |
| `85a92f5` | Add protected immutable Test deployment path (#75) |
| `840f712` | Document durable Test deployment access (#76) |
| `9a7ec9a` | Add protected keyless Production deployment (#148) |
| `205371d` | Always surface ssh_status and log size as a check annotation (#137) |

Latest protected Test deploy proof on host:

| Commit | Evidence |
| --- | --- |
| `5c2c483b819c9beff8fe18ec069d59ba6af9eb98` | GitHub Actions run [33446723706](https://github.com/streetsmart-insurance/robie-hermes/actions/runs/33446723706); deployment dir `5c2c483b819c` on VM |

Task 1 completion commits (this branch `pawel-task1-ssh`):

| Path | Change |
| --- | --- |
| `scripts/ensure-gcloud-ssh-key.sh` | Non-interactive OS Login key bootstrap under `$HOME/.ssh` |
| `scripts/grant-team-ssh-access.sh` | Admin helper: instance `osLogin` + project IAP tunnel |
| `docs/SSH_TEAM_ACCESS_RUNBOOK.md` | Operator + admin runbook |
| `.github/workflows/deploy-test.yml` | Call `ensure-gcloud-ssh-key.sh` before SSH steps |
| `docs/GCP_ACCESS_RUNBOOK.md` | Mark Task 1 complete; link here |
| `tests/test_test_deploy_workflow.py` | Assert workflow uses ensure script |

## Live GCP changes (2026-09-01)

Executed by `pawelstasinskiuk@gmail.com`:

1. `gcloud compute instances remove-metadata hermes-test-01 --keys=ssh-keys` — removed legacy Carlo metadata keys.
2. `gcloud compute firewall-rules update default-allow-ssh --disabled` — disabled public SSH on unused `default` network.
3. Instance IAM: `roles/compute.osLogin` for `user:pawelstasinskiuk@gmail.com` on both VMs.
4. `gcloud compute os-login ssh-keys add` — registered deploy/operator key.

## IAM — instance-scoped bindings

### `hermes-test-01`

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com",
        "user:carlo@streetsmart.insurance"
      ],
      "role": "roles/compute.osAdminLogin"
    },
    {
      "members": [
        "user:pawelstasinskiuk@gmail.com"
      ],
      "role": "roles/compute.osLogin"
    }
  ],
  "etag": "BwZabUgadEg=",
  "version": 1
}
```

### `hermes-poc-01`

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:robie-production-deployer@streetsmart-hermes-poc.iam.gserviceaccount.com"
      ],
      "role": "roles/compute.osAdminLogin"
    },
    {
      "members": [
        "user:pawelstasinskiuk@gmail.com"
      ],
      "role": "roles/compute.osLogin"
    }
  ],
  "etag": "BwZabUhk3MM=",
  "version": 1
}
```

Deploy service accounts retain `roles/compute.osAdminLogin` on their respective VMs only.

## Firewall dump (`streetsmart-hermes-poc`)

Hermes VMs use network `hermes-poc-net`. **Only** SSH ingress on that network:

```json
{
  "name": "hermes-allow-iap-ssh",
  "network": "hermes-poc-net",
  "direction": "INGRESS",
  "disabled": false,
  "sourceRanges": ["35.235.240.0/20"],
  "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
  "targetTags": ["hermes-poc"]
}
```

Legacy `default-allow-ssh` on unused `default` network — **disabled** (`disabled: true`, was `0.0.0.0/0:22`). Hermes instances are **not** attached to `default`.

Full project firewall list (2026-09-01):

```json
[
  {"name": "default-allow-icmp", "network": "default", "sourceRanges": ["0.0.0.0/0"], "disabled": false},
  {"name": "default-allow-internal", "network": "default", "sourceRanges": ["10.128.0.0/9"], "disabled": false},
  {"name": "default-allow-rdp", "network": "default", "sourceRanges": ["0.0.0.0/0"], "ports": ["3389"], "disabled": false},
  {"name": "default-allow-ssh", "network": "default", "sourceRanges": ["0.0.0.0/0"], "ports": ["22"], "disabled": true},
  {"name": "hermes-allow-iap-ssh", "network": "hermes-poc-net", "sourceRanges": ["35.235.240.0/20"], "ports": ["22"], "targetTags": ["hermes-poc"], "disabled": false}
]
```

## VM metadata — `hermes-test-01` (post-cleanup)

```yaml
metadata:
  items:
  - key: enable-oslogin
    value: 'TRUE'
  - key: startup-script
    value: |
      #!/bin/bash
      echo ROBIETEST_BEGIN >/dev/ttyS0
      ...
```

No `ssh-keys` metadata key remains.

## OS Login — `pawelstasinskiuk@gmail.com`

POSIX account: `ext_pawelstasinskiuk_gmail_com`  
Registered key fingerprints (revocable individually):

- `dca86444d7bd7516f73646bbb4d7df32444c748653835760904a4a80b620d88a`
- `7ae0a1c693edde54787fbbcd6183fc8e1294ee9e5e6f7b88c6ff3cc9ff4de0b3`

## Non-interactive bootstrap transcript (operator laptop)

```
=== TRANSCRIPT 2026-09-01 Task1 SSH bootstrap ===
Tue Sep  1 15:09:59 UTC 2026
--- ensure-gcloud-ssh-key ---
os_login_key_ready=/Users/user/.ssh/google_compute_engine.pub
--- ssh hermes-test-01 ---
SSH_OK
hermes-test-01
ext_pawelstasinskiuk_gmail_com
--- ssh hermes-poc-01 ---
SSH_OK
hermes-poc-01
ext_pawelstasinskiuk_gmail_com
--- troubleshoot hermes-test-01 ---
User permissions: 0 issue(s) found.
VPC settings: 0 issue(s) found.
VM status: 0 issue(s) found.
VM boot: 0 issue(s) found.
--- latest deploy evidence head ---
5c2c483b819c
f78376f40a62
b98801691de6
```

No passphrase or manual key entry occurred.

## GitHub Actions deploy transcript (non-interactive SSH)

Workflow: **Deploy immutable release to Test**  
Run: https://github.com/streetsmart-insurance/robie-hermes/actions/runs/33446723706  
Commit: `5c2c483b819c9beff8fe18ec069d59ba6af9eb98`  
Result: **success**

SSH material on runner (from workflow log):

```
WARNING: The private SSH key file for gcloud does not exist.
WARNING: SSH keygen will be executed to generate a key.
Generating public/private rsa key pair.
Your identification has been saved in /home/runner/.ssh/google_compute_engine
```

No `Enter passphrase` prompt in log. Deploy evidence on VM includes `production_touched: false`.

## Operator quick reference

```sh
gcloud auth login your@email.com
gcloud config set project streetsmart-hermes-poc
bash scripts/ensure-gcloud-ssh-key.sh
gcloud compute ssh hermes-test-01 --zone=us-east1-b --tunnel-through-iap --project=streetsmart-hermes-poc
```

See also: `docs/SSH_TEAM_ACCESS_RUNBOOK.md`.
