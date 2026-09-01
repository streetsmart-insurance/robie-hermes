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
- Library path: `robie_job_engine/secret_resolution.py` (`access_newest_enabled_secret`)

`secret()` and `load_ezlynx_credentials()` always list versions with
`state:ENABLED` and access the **newest ENABLED** by `create_time`. They do
**not** call `access_secret_version` on `versions/latest` or a pinned numeric
version when that version is DESTROYED.

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
and ALERTs / HOLDs only when there is no ENABLED version. A DESTROYED
`versions/latest` leftover (password v2 on 468d1575) is healthy when an older
ENABLED version exists (password v1). Chat wording is
`using ENABLED v1; v2 is DESTROYED leftover`, never `password is destroyed`.

## Recurrence guard (code)

Zip path: `robie_job_engine/login_secret_health.py`.

- `open_chat_job` preflight (before RUNNING / recorder).
- Scheduler tick (periodic; skipped when Secret Manager is unavailable).
- No ENABLED → `NEEDS_AUTH` + Chat alert.
- Newest DESTROYED but an older ENABLED exists → healthy (result `OK`). Chat
  may note `using ENABLED v1; v2 is DESTROYED leftover`. Job proceeds.
- Alert / leftover text is states and version names only. Never payloads.

`python -m robie_job_engine.login_secret_health` prints the same state report.
