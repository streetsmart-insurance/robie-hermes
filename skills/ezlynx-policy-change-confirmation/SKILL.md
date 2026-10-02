---
name: "ezlynx-policy-change-confirmation"
description: "Read an assigned EZLynx policy-change task, compare the request, the carrier endorsement, and the EZLynx record, and draft a note for the producer to confirm."
version: "0.1.0-pilot"
status: "Testing"
job_type: "policy_change_confirmation"
production_ready: false
---

# Policy change confirmation (read-only pilot)

Trigger: a CSR assigns an existing policy-change task to ROBIE.

The job resolves one case and the original assigner from the assignment event. It reads the request, the issued endorsement, and the EZLynx record, then drafts a comparison and a note. The note ends with `ROBIE was here`.

The producer confirms and closes. The original assigner receives the result. This pilot does not file the note, upload a file, change a label, reassign, email, submit a portal, change the policy, confirm, or close.

Do not open a new Change Request form. Do not use this job in place of the weekly overdue policy-change checker.

The original request is not always a written Client Center form. If the only source is a call recording, or there is no clear written request with a requested effective date, mark the case `request source unclear (e.g. call recording)` and hold it for a person. Do not guess the date. Do not transcribe a recording.

Also read the EZLynx task: title, description, comments, assignee, due date, and attachments, with the Client Center request and the discussion notes. The task due date is not the requested effective date. If the task and the client request state different effective dates, flag that disagreement. Do not pick one. A task date does not fill in an unclear written request.

Progressive is the provisional carrier. Do not treat a memo retrieval as an issued endorsement. Production stays closed until Test evidence exists.

An endorsement already filed on the EZLynx documents can be read when the live Directory download route cannot be walked. Label that source as an EZLynx-filed endorsement. It does not prove the Directory route and it does not confirm Progressive.

EZLynx has no Task API. Read the task id, due date, assignee, and submission evidence from a snapshot of the task already on screen. Read the vehicle list and the change effective date from a snapshot of the policy. A transaction date is not the change effective date. A discussion note is not submission evidence. Do not invent either one.

Do not build a login that reads or types a For Agents Only email code. That path is design only: `docs/PROGRESSIVE_FAO_EMAIL_OTP.md`.
