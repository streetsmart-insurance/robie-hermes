---
name: "ezlynx-document-upload"
description: "Push a document file into an EZLynx applicant's document library via the DocumentApi. Email/Chat worker tool; the engine enforces the write-allowlist."
job_type: "ezlynx.document_upload"
version: "0.1.0"
status: "Testing"
production_ready: false
---

# EZLynx Document Upload v0.1.0

Use this skill when an email or Chat job needs a file pushed into EZLynx —
dec pages, renewal packets, carrier correspondence, application PDFs, photos.

## The one rule

Call the `ezlynx_document_upload` tool. Never hand-roll the DocumentApi call,
never drive the browser to upload a file. The tool runs the Job Engine path:
it reads the local file, authenticates via Secret Manager, and posts through
the DocumentApi OAuth upload endpoint. It returns the new EZLynx document id.

## Arguments

- `applicant_id` (required): EZLynx applicant/account id.
- `file_path` (required): local filesystem path to an already-downloaded file
  (e.g. an email attachment saved to the job evidence directory). The tool
  fails closed when the path is missing, unreadable, or empty.
- `document_name` (required): display name for the document in EZLynx.
- `policy_master_id` (optional): associate with a policy; defaults to 0
  (applicant-level document).
- `file_content_type` (optional): MIME type, e.g. `application/pdf`.

## Safety contract

- The engine's write-allowlist (`require_allowed_ezlynx_write_applicant`) runs
  inside the upload call. Ops flip: leave `ROBIE_EZLYNX_WRITE_APPLICANT_IDS`
  unset/empty for agency-wide note/document writes; set it to a comma list
  to restrict. A bound Production Chat job still fail-closes to that
  applicant. The tool returns an error, never a partial upload.
- This tool uploads one document. It never binds, never pays, never deletes
  a policy or a document.
- A missing/expired EZLynx API session is a failed tool call, never a
  verified zero. Do not retry blindly; surface the error.
