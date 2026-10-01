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

Progressive is the provisional carrier. Do not treat a memo retrieval as an issued endorsement. Production stays closed until Test evidence exists.
