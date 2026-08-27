# EZLynx login secrets — on-call runbook

Documentation only. **No secret values.** Do not print or store the password.
Pawel owns rotation. Carlo does not need to SSH. Chat HITL is enough.

Job `6cf6f6ae` HITL'd after Secret Manager served a **DESTROYED** version.
This page is the recurrence guard's operator text.

## Where Production reads them

- Host: **hermes-poc-01**
- Project: **streetsmart-hermes-poc**
- Secrets: `ezlynx-username`, `ezlynx-password`
- Live login: zip-loaded `ezlynx_login_bootstrap.py` (`secret()`)

`secret()` lists versions with `state:ENABLED` and accesses the **newest
ENABLED** by `create_time`. It does **not** use `versions/latest`.

`versions/latest` is the newest version number, including DESTROYED. That is
how a live password can look present while login HITLs.

## Observed 2026-08-27 (states only)

| Secret | Newest version | State | Notes |
| --- | --- | --- | --- |
| `ezlynx-password` | v2 | DESTROYED | `latest` is dead |
| `ezlynx-password` | v1 | ENABLED | bootstrap uses this |
| `ezlynx-username` | v3 | ENABLED | login id SSRobie |

A DESTROYED version **cannot be restored**.

## Replace and RETRY

1. Add a **new ENABLED** version (or enable an older ENABLED one). Pawel.
   `skills/ezlynx-session-login/scripts/provision-secrets.sh` adds versions
   without echoing the password.
2. Reply **RETRY** in the Robie Chat HITL thread.

Chat HITL text does **not** change the Secret Manager version. Bootstrap
re-reads newest ENABLED on resume.

Never paste the password into Chat, the Job ledger, a recording note, or this
repo.

## Test VM

**hermes-test-01** is a different VM. Same project/secret *names* only if that
VM's service account has `secretAccessor` on those secrets. Do not assume Test
can read Production versions.

## Automatic rotation from EZLynx

**Not possible from this repo.** There is no EZLynx-side password-rotation
webhook or callback here. Do not invent one. The recurrence guard is a
preflight / scheduler check that lists **version states** (never payloads)
and ALERTs in Chat if there is no ENABLED version or the newest version is
DESTROYED — before a Chat job is mid-run.

## Recurrence guard (code)

Zip path: `robie_job_engine/login_secret_health.py`.

- `open_chat_job` preflight (before RUNNING / recorder).
- Scheduler tick (periodic; skipped when Secret Manager is unavailable).
- No ENABLED → `NEEDS_AUTH` + Chat alert. Newest DESTROYED but an older
  ENABLED exists → Chat alert, job may proceed (bootstrap still has a version).
- Alert text is states and version names only.

`python -m robie_job_engine.login_secret_health` prints the same state report.
