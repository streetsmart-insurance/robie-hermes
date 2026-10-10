# EZLynx write-scope policy: install runbook

Carlo approved 2026-10-10 (5:54 PM ET): robie-filer, and later the
`ezlynx-api` CLI, may upload documents and append notes to **any** client.
Nothing else widens. This runbook installs, checks and removes the one file
that turns it on. **Installing the file on Production needs Carlo's explicit
go.** The release that carries this code does not install it.

## What it is

A root-owned JSON file, `/etc/streetsmart-hermes/ezlynx-write-scope.json`.

- **No file = closed.** Everything behaves as before: only the test client
  `220250093` can be written.
- It allows two operations only, `document_upload` and `note_append`. Any
  other word in the file makes the whole file refused.
- It names the **entrypoints** that get them: `robie_filer` and
  `ezlynx_api_cli`. They are switched separately. A process that is not named
  gets nothing. The sealed agent interpreter can never register it.
- No environment variable, flag, job payload or chat message can turn it on or
  change where it is read from. `ROBIE_EZLYNX_WRITE_SCOPE=all` stays refused.
- The code reads it once at start. A change takes effect on the next process
  (the filer timer starts a new process each run; the CLI is one process per
  command).

What it does **not** change: the driver lease (`PRODUCTION`), the audit log,
duplicate guards, read-back, the note ledger, the phone-number filter, no
deletes, the job-type production hold (`job_type_not_production_ready`),
the sealed agent interpreter refusal, Production Chat job limits (a Chat job
stays bound to its one applicant), creating policies, creating discussions,
tasks, labels, reassignment and every browser save. Those keep the applicant
allowlist.

## File format (version 1)

```json
{
  "version": 1,
  "environment": "PRODUCTION",
  "approved_by": "Carlo Ferrara",
  "approved_at": "2026-10-10T17:54:00-04:00",
  "entrypoints": {
    "robie_filer": { "operations": ["document_upload", "note_append"] }
  }
}
```

Every key is required, unknown keys are refused. `environment` must equal the
process's `ROBIE_ENV` and the host (`PRODUCTION` = `hermes-poc-01`, `TEST` =
`hermes-test-01`). Add `"ezlynx_api_cli": { "operations": [...] }` to
`entrypoints` only when Carlo approves the CLI separately. A copy to start from
is `deploy/ezlynx-write-scope.example.json` (filer only).

The loader refuses the file (and the write) when: it is a symlink or not a
regular file, it or `/etc/streetsmart-hermes` is not owned by root or is
group/world writable, it is over 8 KB, it is not valid JSON, any key,
operation or entrypoint is unknown, an operation is listed twice, the
environment or host does not match, or `approved_by`/`approved_at` are
missing. A damaged file never widens anything. The filer stops; the CLI
refuses writes with `write_scope_policy_refused`.

## What gets audited

Each run that registers the scope logs one WARNING line
(`EZLynx operation scope registered: entrypoint=... operations=...
policy_sha256=... policy=<the whole file>`). The filer also logs the hash,
operations, `approved_by` and `approved_at`. Every `ezlynx-api` write line in
the audit log carries `write_scope_policy_sha256`, `write_scope_operations`
and `write_scope_approved_by`. Record the sha256 in the change note.

## Install (Production, only with Carlo's go)

Order matters. Do not skip the dry run.

1. Release with this code is current and healthy; the filer unit is installed
   with the timer stopped (see `docs/ROBIE_FILER.md`).
2. Confirm no policy exists, and the directory is root-owned and not
   group/world writable:

   ```
   sudo stat -c '%U:%G %a %n' /etc/streetsmart-hermes
   sudo test -e /etc/streetsmart-hermes/ezlynx-write-scope.json && echo EXISTS || echo none
   ```

   Expected: `root:streetsmart-hermes 750`, `none`.
3. Prove it is closed first. This must stop with a message naming the policy:

   ```
   sudo -u streetsmart-hermes env ROBIE_ENV=PRODUCTION \
     /opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.robie_filer --any-applicant
   ```

4. Write the file (filer only), root-owned, readable by the service user,
   writable by nobody else:

   ```
   sudo install -o root -g streetsmart-hermes -m 0640 /dev/null /etc/streetsmart-hermes/ezlynx-write-scope.json
   sudo tee /etc/streetsmart-hermes/ezlynx-write-scope.json >/dev/null <<'JSON'
   {
     "version": 1,
     "environment": "PRODUCTION",
     "approved_by": "Carlo Ferrara",
     "approved_at": "<ISO time of Carlo's go>",
     "entrypoints": { "robie_filer": { "operations": ["document_upload", "note_append"] } }
   }
   JSON
   sudo stat -c '%U:%G %a %n' /etc/streetsmart-hermes/ezlynx-write-scope.json   # root:streetsmart-hermes 640
   sudo python3 -m json.tool /etc/streetsmart-hermes/ezlynx-write-scope.json >/dev/null && echo json-ok
   sudo sha256sum /etc/streetsmart-hermes/ezlynx-write-scope.json              # record this
   ```

5. **Filer dry run first** (no `--live`): reads, matches and logs what it
   would file, writes nothing. Check the log shows the policy line with the
   recorded sha256:

   ```
   sudo -u streetsmart-hermes env ROBIE_ENV=PRODUCTION \
     /opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.robie_filer --any-applicant --auto --since-days 1
   ```

6. Going live is a separate step with its own approval: the reviewed
   `40-auto-any-applicant.conf` drop-in (`--live --auto --any-applicant`).
   The installer never installs it.

## Turn it off / roll back

```
sudo rm /etc/streetsmart-hermes/ezlynx-write-scope.json
```

Takes effect for the next process. A run already in flight keeps what it
loaded until it exits; stop it with `sudo systemctl stop robie-filer.service`
if needed. To switch off only one entrypoint, remove its entry from the file
(keep at least one entrypoint or delete the file). Rollback of the release does
not touch the file.

## Test host

Same file, `"environment": "TEST"`, on `hermes-test-01` (`ROBIE_ENV=TEST`).
Test has no live EZLynx path, so use it only to exercise the loader and the
filer dry run. The offline tests are `tests/test_ezlynx_operation_scope.py`.
