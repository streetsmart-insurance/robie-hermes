# robie-filer

Files robie@ email and attachments into EZLynx Documents, every 10 minutes,
without a browser. Requested and approved by Carlo on 2026-10-08.

## What it does

| Source | What happens |
|---|---|
| Sheet "File to EZLynx", tab **Queue** | `ATTACHMENT` rows upload one Gmail attachment. `EMAIL` rows upload the email as a PDF, then each attachment. The row gets `Filed` + document id(s), or `Error` + the exact error. A `discussion_id` also gets the note `Saved to Documents: <document_name>`. |
| Tab **Needs review** | A row with `resolved_applicant_id` filled and `resolved_by` blank is filed to that client. `resolved_by` gets the ids or the error. |
| New robie@ mail (`--auto`) | Matched by policy number, then client email, then thread. One client: filed. Zero or several: a Needs review row. Applied Reporting, staff-only, auto replies and Zapier alerts are skipped. |
| Every PDF filed | Read on the host (`pdftotext`, `tesseract` for pages without text). A dec-page summary with page numbers is saved to `/var/lib/robie-filer/summaries/<document_id>.json` and a one-line version goes on the **Filed** tab. No model is called. |

Only robie@ is read (Carlo 2026-10-08). A calendar reminder for 2026-11-12
asks whether to add producers' mailboxes.

## Safety

- Dry run unless `--live`. A dry run makes no upload, note, or sheet write.
- Uploads: `upload_document_via_api` (DocumentApi + fresh search read-back of
  the new id). Notes: `file_note_to_existing_discussion` (existing discussion
  only, note ledger, read-back). No browser, per `EZLYNX_NOTES_DOCS_API_ONLY`.
- A SQLite ledger (`/var/lib/robie-filer/robie-filer.db`) keys every document
  by Gmail message id + attachment, so nothing is filed twice. An upload that
  started but was not confirmed is settled next run by searching the client's
  documents for the exact name before uploading again.
- Gmail is read-only (DWD client `112650695780807418521`, `gmail.readonly`).
- Which client may receive a write is still decided by `ezlynx_write_scope`.

## Rollout

1. **Merged + Test release**: unit installed, timer stopped, dry run only.
2. **Step 1 (approved)**: `30-live.conf` — Queue and Needs review are live.
   Writes reach only the compiled allowlist (test account `220250093`).
   QA: one `ATTACHMENT` row and one `EMAIL` row with a `discussion_id` on
   220250093; check the document ids and the note in EZLynx.
3. **Step 2 (needs approval after step 1 evidence)**: the write-scope policy
   file below, a filer dry run, then `40-auto-any-applicant.conf` (`--live --auto --any-applicant`).
   The first auto run sets a start mark; older mail is never back-filed.

## Install commands (run on the host, after the release is current)

```
# Install, one verification dry run, timer stopped:
sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh --release-dir /opt/streetsmart-hermes/current
# Step 1 live on the 10-minute timer (after Carlo's go):
sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh --release-dir /opt/streetsmart-hermes/current --live --enable-timer
# Rollback (prints the backup path on install):
sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh --rollback /root/robie-filer-<stamp>
```

The installer never installs `40-auto-any-applicant.conf` and removes it if
present, so step 2 always needs its own reviewed change.

## Write-scope policy (any client, two operations)

The write gate used to pick applicants, not actions. `--any-applicant` now
gets a process-scoped allowance for exactly two operations, `document_upload`
and `note_append`, and only when the root-owned file
`/etc/streetsmart-hermes/ezlynx-write-scope.json` lists `robie_filer`.
Without the file, `--any-applicant` stops before touching anything.
Everything else (policy create, discussion create, tasks, deletes, browser
saves) keeps the applicant allowlist. Code: `ezlynx_write_scope.py`
(`register_filer_operation_scope`, `operation_is_write_allowed`), with the
`operation=` argument on the three note/document write call sites and a seal
check that the agent interpreter cannot register or plant a scope.

Install, check, dry run and rollback: `docs/EZLYNX_WRITE_SCOPE_POLICY_RUNBOOK.md`.
Installing the file on Production needs Carlo's explicit approval and starts
with a filer dry run.
