# Task 2 — Secret Manager version discipline evidence

**Requirement:** Production must always resolve to the **newest ENABLED** secret
version, never a stale `versions/latest` pointer (root cause of 2026-09-01 outage).

**Project:** `streetsmart-hermes-poc`  
**Production host:** `hermes-poc-01`  
**Evidence date:** 2026-09-01 UTC

## Where resolution happens

| Path | Function | Behavior |
| --- | --- | --- |
| `robie_job_engine/secret_resolution.py` | `access_newest_enabled_secret()` | Canonical resolver: strips any `/versions/...` suffix, lists `state:ENABLED`, accesses newest by `create_time` |
| `ezlynx_login_bootstrap.py` | `secret()` | Production EZLynx login bootstrap (zip on `hermes-poc-01`) |
| `robie_job_engine/secret_manager.py` | `load_ezlynx_credentials()` | Gateway/session code path |
| `robie_job_engine/login_secret_health.py` | `inspect_login_secrets()` | Preflight / scheduler state-only guard (never reads payloads) |
| `robie_job_engine/production_preflight.py` | `check_login_secrets()` | Production preflight check #7 |

Env vars (`ROBIE_EZLYNX_USERNAME_SECRET`, `ROBIE_EZLYNX_PASSWORD_SECRET`) may
name the secret parent or include a stale `versions/latest` / numeric suffix.
Resolution **always** ignores the suffix and reads newest ENABLED.

## Live GCP version states (2026-09-01)

```text
$ gcloud secrets versions list ezlynx-password --project=streetsmart-hermes-poc
NAME  STATE      CREATED
2     destroyed  2026-08-24T05:21:10
1     enabled    2026-08-19T15:16:53

$ gcloud secrets versions list ezlynx-username --project=streetsmart-hermes-poc
NAME  STATE      CREATED
3     enabled    2026-08-24T18:26:41
2     destroyed  2026-08-24T05:23:54
1     destroyed  2026-08-19T15:16:05
```

Interpretation:

- `ezlynx-password`: **`versions/latest` aliases v2 (DESTROYED)**. Production must use **v1 ENABLED**.
- `ezlynx-username`: newest overall is **v3 ENABLED** (healthy).

Destroying non-latest versions does **not** remove the ENABLED credential:

- Password v2 destroyed → v1 remains ENABLED → login still works.
- Username v1 and v2 destroyed → v3 remains ENABLED → login still works.

## Proof destroying non-latest does not break Production

### Logical proof (aligned with live states)

| Secret | Newest version (`latest`) | State | Resolved ENABLED | Production impact if using `latest` |
| --- | --- | --- | --- | --- |
| `ezlynx-password` | v2 | DESTROYED | v1 | **Outage** (morning incident) |
| `ezlynx-password` | v2 | DESTROYED | v1 | **OK** with newest-ENABLED resolver |
| `ezlynx-username` | v3 | ENABLED | v3 | OK |

### Automated tests (local)

```text
$ PYTHONPATH=. python3 -m pytest \
    tests/test_secret_resolution.py \
    tests/test_ezlynx_login_secret_versions.py \
    tests/test_login_secret_health.py \
    tests/test_ezlynx_session.py -q
25 passed
```

Key cases:

- `test_newest_enabled_ignores_destroyed_latest_pointer` — request `versions/latest`, access `versions/1`
- `test_pinned_destroyed_numeric_version_still_resolves_enabled` — request `versions/2`, access `versions/1`
- `test_destroyed_latest_with_enabled_older_is_healthy` — health guard OK, no HOLD

### Resolution transcript (simulated Production states)

```python
# newest overall = versions/2 DESTROYED, newest ENABLED = versions/1
access_newest_enabled_secret(client, "projects/p/secrets/ezlynx-password/versions/latest")
# → access_secret_version(name="projects/p/secrets/ezlynx-password/versions/1")
```

No payload printed; states only in `describe_newest_enabled_resolution()`.

## Configuration discipline

Updated examples (no `versions/latest`):

- `deploy/systemd/robie-ezlynx.env.example`
- `deploy/systemd/robie-accountability.env.example`

Use secret **parents** only:

```env
ROBIE_EZLYNX_PASSWORD_SECRET=projects/streetsmart-hermes-poc/secrets/ezlynx-password
```

Even if an operator leaves `versions/latest` in env, runtime ignores it.

## Related commits

| Commit | Description |
| --- | --- |
| `5c2c483` | **Regression risk:** added pinned version env path (#163) |
| `a5729c3` | Secret Manager-backed EZLynx session skill (#7) |
| *(this branch)* | Centralize newest-ENABLED resolver; remove stale pin behavior |

### This branch changes

| File | Change |
| --- | --- |
| `robie_job_engine/secret_resolution.py` | New canonical resolver |
| `ezlynx_login_bootstrap.py` | Always calls resolver |
| `robie_job_engine/secret_manager.py` | `access_newest_enabled()` for EZLynx |
| `deploy/systemd/*.env.example` | Remove `versions/latest` |
| `LOGIN_SECRETS.md` | Document resolver module |
| `tests/test_secret_resolution.py` | Destroyed-latest proof tests |

## Operator commands

```sh
# State-only report (no payloads)
PYTHONPATH=. python3 -m robie_job_engine.login_secret_health

# Version states via gcloud (no payload access required)
gcloud secrets versions list ezlynx-password --project=streetsmart-hermes-poc
gcloud secrets versions list ezlynx-username --project=streetsmart-hermes-poc
```

See also: `LOGIN_SECRETS.md`, `docs/SSH_TEAM_ACCESS_RUNBOOK.md`.
