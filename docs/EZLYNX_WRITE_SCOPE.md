# EZLynx write applicant scope

Carlo 2026-09-17: Robie must read and write notes/documents for any
applicant agency-wide when running as the automation identity. Bind,
cancel, coverage change, and money moves stay blocked by other guards.

## All clients

Default is closed. Opening every real applicant is an explicit setting:

```
ROBIE_EZLYNX_WRITE_SCOPE=all
```

`ROBIE_EZLYNX_WRITE_APPLICANT_IDS=*` means the same request. It is honored
only while the Playground is on and its hard blocks, read-back-then-go,
and undo log are active. If those are not active, Robie uses the id list,
or test account `220250093` when that list is empty. This does not allow
bind, delete, billing, coverage changes, or email or text to a client.

The Prod flip, later, after QA, is `ROBIE_PLAYGROUND=1` together with
`ROBIE_EZLYNX_WRITE_SCOPE=all`. This pull request does not set it.

## Ops flip

The compiled allowlist is read once at process start from
`ROBIE_EZLYNX_WRITE_APPLICANT_IDS` (preferred) or the legacy alias
`EZLYNX_WRITE_APPLICANT_IDS`. A job payload cannot widen it.

| Goal | Setting |
| --- | --- |
| **Agency-wide** (default) | Leave the variable **unset or empty**. Any plausible EZLynx applicant ID is eligible for already write-scoped calls (note append, document upload, PolicyApi writes that already go through `require_allowed_ezlynx_write_applicant`). |
| **Restricted** | Set a comma list, e.g. `ROBIE_EZLYNX_WRITE_APPLICANT_IDS=220250093` or `220250093,221111111`. Only those IDs pass the compiled allowlist. |

Invalid IDs (`""`, `SANITIZED-001`, `0`) are always refused.

## Production Chat jobs stay fail-closed

When `production_job_applicant()` returns a bound applicant on the real
Production host/release, writes are limited to **that** applicant even if
the compiled allowlist is unrestricted. That is intentional Chat-job
safety. Agency-wide API workers that are not applicant-bound use the env
policy above.

## What this does not enable

- Bind, cancel, coverage change, or money moves
- Deletes (cardinal no-delete rule)
- Playwright FormEntry on ROBIE Test LLC without the synthetic
  Homeowners / `TEST-HO-*` / `$1.00` header attestation

FormEntry uses the same env allowlist for *which* applicant may be
targeted. The Test-account header checks remain for `220250093`.

## Where it is enforced

- `robie_job_engine/ezlynx_write_scope.py`
- Note append (`ezlynx_discussions`) and document upload (`ezlynx_api`)
- Playwright generic writes and FormEntry (`playwright_write_guard.py`)

EZLynx notes and documents themselves are API-only (Carlo 2026-09-19).
Playwright must never file a note or upload a document. See
`docs/EZLYNX_NOTES_DOCS_API_ONLY.md`.
