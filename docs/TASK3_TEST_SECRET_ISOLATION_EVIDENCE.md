# Task 3 — Test secret isolation evidence

**Requirement:** `hermes-test-01` must have **zero access** to Production secrets under
any failure mode.

**Test VM:** `hermes-test-01` (`us-east1-b`, project `streetsmart-hermes-poc`)  
**Evidence date:** 2026-09-01 UTC

## Test service account

| Field | Value |
| --- | --- |
| Attached to `hermes-test-01` | `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| OAuth scope | `https://www.googleapis.com/auth/cloud-platform` |
| Production VM SA (contrast) | `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` |

Project-level IAM for the Test SA could not be exported (`getIamPolicy` denied for
reviewer). **Secret-level IAM** (authoritative for EZLynx reads) is below.

## Secret-level IAM policy (Test SA bindings)

### `ezlynx-username` — **MUST BE REMOVED**

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com"
      ],
      "role": "roles/secretmanager.secretAccessor"
    }
  ]
}
```

### `ezlynx-password` — **MUST BE REMOVED**

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com"
      ],
      "role": "roles/secretmanager.secretAccessor"
    }
  ]
}
```

### `robie-google-oauth-token` — **MUST BE REMOVED**

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com"
      ],
      "role": "roles/secretmanager.secretAccessor"
    }
  ]
}
```

### `hermes-api-token` — **correctly isolated** (Production SA only)

```json
{
  "bindings": [
    {
      "members": [
        "serviceAccount:hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com"
      ],
      "role": "roles/secretmanager.secretAccessor"
    }
  ]
}
```

## Live read attempts from `hermes-test-01` (2026-09-01)

### BEFORE IAM fix — isolation **BROKEN**

```text
$ bash scripts/prove-test-production-secret-denial.sh
host=hermes-test-01 service_account=robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com
FAIL ezlynx-username: read succeeded (isolation broken)
```

```text
$ gcloud compute ssh hermes-test-01 ... --command 'gcloud secrets versions access latest --secret=ezlynx-username ...'
SSRobie
exit=0
```

```text
$ python via /opt/streetsmart-hermes-test/venv/bin/python (Secret Manager API)
READ_OK 7
```

Test VM **successfully read** Production `ezlynx-username` payload (7 bytes).

### AFTER IAM fix — **CONFIRMED** (2026-09-01)

Carlo (Cloud Shell) and reviewer both ran `prove-test-production-secret-denial.sh`:

```text
host=hermes-test-01 service_account=robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com
DENIED ezlynx-username: exit=1 stderr=PERMISSION_DENIED ... secretmanager.versions.access ...
DENIED ezlynx-password: exit=1 stderr=PERMISSION_DENIED ... secretmanager.versions.access ...
PASS: Production secrets denied on Test VM
```

Secret-level IAM: Test SA bindings **removed** from `ezlynx-username`, `ezlynx-password`,
`robie-google-oauth-token`.

Admin revoke (idempotent):

```sh
bash scripts/isolate-test-from-production-secrets.sh
bash scripts/prove-test-production-secret-denial.sh
```

## Runtime fail-closed backstop (defense in depth)

Even if IAM is misconfigured again, Test runtime refuses Production secret IDs:

| Module | Guard |
| --- | --- |
| `robie_job_engine/secret_isolation.py` | `forbid_test_reading_production_secrets()` when `ROBIE_ENV=TEST` or hostname `hermes-test-01` |
| `robie_job_engine/secret_resolution.py` | Calls guard before any `access_secret_version` |

Blocked Production-only secret IDs: `ezlynx-username`, `ezlynx-password`,
`robie-google-oauth-token`.

## CI gate

`.github/workflows/diagnose-test-secret-access.yml` now **fails** when Test VM can
read Production EZLynx secrets (was previously always green).

## Related commits

| Commit | Description |
| --- | --- |
| `a00a587` | Keyless GCP readiness audit (#70) |
| `85a92f5` | Protected Test deployment (#75) |
| *(this branch)* | Runtime isolation guard + IAM revoke/prove scripts + evidence |

### This branch changes

| Path | Change |
| --- | --- |
| `robie_job_engine/secret_isolation.py` | Test runtime fail-closed guard |
| `robie_job_engine/secret_resolution.py` | Wire guard before secret access |
| `scripts/isolate-test-from-production-secrets.sh` | Admin: revoke Test SA on Production secrets |
| `scripts/prove-test-production-secret-denial.sh` | Live denial proof from Test VM |
| `.github/workflows/diagnose-test-secret-access.yml` | Fail when reads succeed |
| `tests/test_secret_isolation.py` | Automated guard tests |

## Checklist

| Item | Status |
| --- | --- |
| Test SA identified | **DONE** |
| IAM policy exported (secret-level) | **DONE** |
| Live denied attempt | **DONE** (Carlo Cloud Shell + reviewer, exit=1 PERMISSION_DENIED) |
| Runtime fail-closed on Test | **DONE** (code) |
| CI fails on isolation break | **DONE** |
