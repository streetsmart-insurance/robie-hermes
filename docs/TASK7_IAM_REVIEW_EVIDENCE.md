# Task 7 — IAM review evidence

**Requirement:** Every account / service account audited to minimum necessary access.

**Project:** `streetsmart-hermes-poc`  
**Evidence date:** 2026-09-01 UTC  
**Reviewer:** `pawelstasinskiuk@gmail.com`

## Audit coverage

| Layer | Readable by reviewer | Status |
| --- | --- | --- |
| Service account inventory | Yes | **7 SAs** listed below |
| Instance IAM (`hermes-poc-01`, `hermes-test-01`) | Yes | **Exported** |
| Secret IAM (per-secret) | Partial | Production secrets: bindings empty or hermes-poc only |
| Project IAM (`getIamPolicy`) | **No** (`403`) | **UNVERIFIED** — admin must run `audit-hermes-iam.sh` |
| SA resource IAM (`getIamPolicy`) | **No** | **UNVERIFIED** |

---

## Full account / role list (verified + inferred)

### Service accounts

| Account | Display name | Attached VM | OAuth scopes (live) | Verified roles / access |
| --- | --- | --- | --- | --- |
| `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` | Hermes POC | `hermes-poc-01` | `cloud-platform`, `spreadsheets`, `drive.file` | Secret accessor on `api-server-key`, `hermes-api-token` (verified). Project roles: **UNVERIFIED** |
| `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` | ROBIE Test Drive Reader | `hermes-test-01` | `cloud-platform` | **No** Production secret access (live denial). Project roles: **UNVERIFIED** |
| `robie-production-deployer@streetsmart-hermes-poc.iam.gserviceaccount.com` | ROBIE Production deployer | — | — | `roles/compute.osAdminLogin` on `hermes-poc-01` (instance IAM). WIF: `github-production` pool. Project roles: **UNVERIFIED** |
| `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com` | (Test project) | — | — | `roles/compute.osAdminLogin` on `hermes-test-01` (instance IAM). WIF: `github-actions` pool in `streetsmart-robie-test` |
| `robie-chat-build@streetsmart-hermes-poc.iam.gserviceaccount.com` | ROBIE Chat bridge build | — | — | **UNVERIFIED** — audit required |
| `robie-chat-bridge@streetsmart-hermes-poc.iam.gserviceaccount.com` | ROBIE Google Chat bridge | — | — | **UNVERIFIED** — audit required |
| `claude-cloud-diag@streetsmart-hermes-poc.iam.gserviceaccount.com` | claude-cloud-diag | — | — | **UNVERIFIED** — audit required |
| `751771086524-compute@developer.gserviceaccount.com` | Default Compute SA | **Not attached** to Hermes VMs | — | Must remain unused on Hermes |

### Human users (instance IAM — verified)

| User | `hermes-test-01` | `hermes-poc-01` | Project IAM |
| --- | --- | --- | --- |
| `carlo@streetsmart.insurance` | `roles/compute.osAdminLogin` | — | **UNVERIFIED** (likely billing owner) |
| `pawelstasinskiuk@gmail.com` | `roles/compute.osLogin` | `roles/compute.osLogin` | **UNVERIFIED** (`iap.tunnelResourceAccessor` required for IAP SSH — works live) |

### Instance IAM — full export (2026-09-01)

**`hermes-test-01`**

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
      "members": ["user:pawelstasinskiuk@gmail.com"],
      "role": "roles/compute.osLogin"
    }
  ]
}
```

**`hermes-poc-01`**

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
      "members": ["user:pawelstasinskiuk@gmail.com"],
      "role": "roles/compute.osLogin"
    }
  ]
}
```

### Secret IAM — Production secrets (2026-09-01)

| Secret | Bindings visible to reviewer |
| --- | --- |
| `ezlynx-username` | **No bindings** (Test SA removed) |
| `ezlynx-password` | **No bindings** (Test SA removed) |
| `robie-google-oauth-token` | **No bindings** (Test SA removed) |
| `hermes-api-token` | `hermes-poc@` → `roles/secretmanager.secretAccessor` |
| `api-server-key` | `hermes-poc@` → `roles/secretmanager.secretAccessor` |

### GitHub WIF identities (from workflows)

| Workflow identity | Impersonates | Scope |
| --- | --- | --- |
| Test workflows | `robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com` | Test deploy + diagnostics |
| Production deploy | `robie-production-deployer@streetsmart-hermes-poc.iam.gserviceaccount.com` | `hermes-poc-01` only |

### Network / SSH posture (verified)

- Hermes VMs on `hermes-poc-net`, tag `hermes-poc`
- Only SSH ingress: `hermes-allow-iap-ssh` → `35.235.240.0/20:22`
- `default-allow-ssh` on unused `default` network: **disabled**
- OS Login enabled on both VMs; no `ssh-keys` metadata on `hermes-test-01`

---

## Live isolation proof (2026-09-01)

```text
$ bash scripts/prove-test-production-secret-denial.sh
host=hermes-test-01 service_account=robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com
DENIED ezlynx-username: exit=0 stderr=PERMISSION_DENIED ... secretmanager.versions.access ...
DENIED ezlynx-password: exit=0 stderr=PERMISSION_DENIED ... secretmanager.versions.access ...
PASS: Production secrets denied on Test VM
```

---

## What we changed

| # | Change | Actor | When | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Removed legacy `ssh-keys` metadata from `hermes-test-01` | Pawel | Task 1 | No `ssh-keys` in metadata |
| 2 | Disabled `default-allow-ssh` (public `0.0.0.0/0:22` on unused network) | Pawel | Task 1 | `disabled: true` |
| 3 | Instance `roles/compute.osLogin` for `pawelstasinskiuk@gmail.com` on both VMs | Pawel | Task 1 | Instance IAM dumps above |
| 4 | Revoked `secretAccessor` for `robie-test-drive-reader@` on `ezlynx-username`, `ezlynx-password`, `robie-google-oauth-token` | Admin (Carlo) | Task 3 / before this audit | Live denial + empty secret IAM |
| 5 | Re-verified `default-allow-ssh` disabled | Pawel | Task 7 | `gcloud compute firewall-rules update --disabled` |
| 6 | Added audit/harden scripts + expected minimum manifest | Pawel | Task 7 | This branch |

**Not changed (recommended follow-up):**

- Narrow VM OAuth scopes off `cloud-platform` (requires VM stop)
- Audit / trim project roles on `robie-chat-*`, `claude-cloud-diag`
- Full project IAM export (reviewer lacks `getIamPolicy`)

---

## Admin: complete the audit

```sh
bash scripts/audit-hermes-iam.sh
# Paste account-role-summary.md into this file after review
bash scripts/harden-hermes-iam.sh   # idempotent remediations
```

---

## Checklist

| Item | Status |
| --- | --- |
| SA inventory documented | **DONE** |
| Instance IAM exported | **DONE** |
| Secret isolation verified live | **DONE** |
| Project IAM full export | **PENDING** admin |
| Chat/diag SA roles reviewed | **PENDING** admin |
| VM OAuth scopes narrowed | **PENDING** maintenance |

## Related commits

| Commit | Description |
| --- | --- |
| *(this branch)* | IAM manifest, audit/harden scripts, evidence |
