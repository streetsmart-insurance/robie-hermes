# EZLynx notes and documents are API only

Carlo 2026-09-19: if something would save a note or a document to EZLynx, it
MUST use the Notes/Discussion API and the Document API.

## Rule

- **Notes:** `add_note_to_discussion` /
  `file_note_to_existing_discussion` (DiscussionApi). Append to an existing
  titled discussion only. Never Untitled. Read back `note_id` before
  success.
- **Documents:** `EzlynxApiClient.upload_applicant_document` /
  `ezlynx_document_upload` (DocumentApi). Read back the numeric
  `document_id` on a fresh document-search before success.
- **Playwright / CDP:** forms and carrier portals only. Playwright must never
  file EZLynx notes or upload EZLynx documents. A Playwright note/doc path
  raises `PLAYWRIGHT_BLOCKED` / `EZLYNX_NOTE_DOC_API_ONLY` and cannot
  authorize `COMPLETE`.
- **COMPLETE:** a claimed note write without a DiscussionApi `note_id` (or
  `ezlynx_note_id`) is refused. A claimed document write without a
  DocumentApi `document_id` is refused.

Chat workers call `ezlynx_discussion_note` and `ezlynx_document_upload`.
Do not use a browser file chooser or the Add Note / Save Note pane.

No Production deploy, Bland dial, or bind is implied by this rule.
