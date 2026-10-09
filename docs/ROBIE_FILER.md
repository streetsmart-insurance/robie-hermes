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
3. **Step 2 (needs approval after step 1 evidence)**: the write-scope patch
   below, then `40-auto-any-applicant.conf` (`--live --auto --any-applicant`).
   The first auto run sets a start mark; older mail is never back-filed.

## Write-scope patch (not in this branch)

The write gate picks applicants, not actions. `--any-applicant` needs a
process-scoped allowance for exactly two operations. This change touches the
EZLynx write gate, so it is held for Carlo to apply or authorize separately.

```diff
--- robie_job_engine/ezlynx_write_scope.py
+OPERATION_DOCUMENT_UPLOAD = "document_upload"
+OPERATION_NOTE_APPEND = "note_append"
+FILER_OPERATIONS = frozenset({OPERATION_DOCUMENT_UPLOAD, OPERATION_NOTE_APPEND})
+_ANY_APPLICANT_OPERATIONS: frozenset[str] | None = None
+
+def register_filer_operation_scope() -> None:
+    """robie_filer main only. Refused inside the sealed agent interpreter."""
+    from .safety_seal import agent_interpreter
+    if agent_interpreter():
+        raise EzlynxWriteScopeError(f"{EZLYNX_WRITE_SCOPE_REFUSED}: filer scope refused in agent interpreter")
+    global _ANY_APPLICANT_OPERATIONS
+    _ANY_APPLICANT_OPERATIONS = FILER_OPERATIONS
+
+def operation_is_write_allowed(value: object, operation: str | None) -> bool:
+    if operation is None or _ANY_APPLICANT_OPERATIONS is None or operation not in _ANY_APPLICANT_OPERATIONS:
+        return False
+    applicant = normalize_applicant_id(value)
+    return is_plausible_applicant_id(applicant) and production_job_applicant() is None
@@ require_allowed_ezlynx_write_applicant
-def require_allowed_ezlynx_write_applicant(value: object) -> str:
+def require_allowed_ezlynx_write_applicant(value: object, *, operation: str | None = None) -> str:
     ...
     applicant_id = normalize_applicant_id(value)
+    if operation_is_write_allowed(applicant_id, operation):
+        return applicant_id
     if not applicant_is_write_allowed(applicant_id):
--- robie_job_engine/ezlynx_api.py  (upload_applicant_document)
-        applicant = require_allowed_ezlynx_write_applicant(applicant_id)
+        applicant = require_allowed_ezlynx_write_applicant(applicant_id, operation=OPERATION_DOCUMENT_UPLOAD)
--- robie_job_engine/ezlynx_discussions.py  (append_note, file_note_to_existing_discussion)
-        require_allowed_ezlynx_write_applicant(applicant_id)
+        require_allowed_ezlynx_write_applicant(applicant_id, operation=OPERATION_NOTE_APPEND)
--- robie_job_engine/safety_seal.py
+    snapshot and compare _ANY_APPLICANT_OPERATIONS and the code of
+    operation_is_write_allowed / register_filer_operation_scope, like allow_obj
```

Unchanged by design: policy create, discussion create, task create, and every
browser save pass no `operation` and keep the applicant allowlist.

## Known limits

- The email PDF is plain text (Helvetica, Latin-1). Images and formatting in
  the body are not reproduced; attachments are filed as originals.
- The sheet's `label` column is kept but not sent: DocumentApi upload takes
  only DocumentName, File, and PolicyMasterId.
- Policy search returns `policyId`; it is sent as `PolicyMasterId`. Step 1 QA
  must confirm the document lands under the right policy.
- Task reassign and due-date change have no EZLynx API and are not part of
  this job.
