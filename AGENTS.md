# ROBIE Engineering operating contract

These instructions apply to every human or AI agent working in this repository.

## Read before acting

Read `CURRENT_STATE.md`, `HANDOFF.md`, `RELEASE_PROCESS.md`, `APPROVALS.md`,
`CLOUD_DESK.md`, `.agents/rules/robie-production-safety.md`, and the relevant
workflow under `.agents/workflows/` before changing code or infrastructure.
Documentation is not deployment proof. Verify GitHub, Test, and Production
independently and record immutable identifiers.

## Engineering / Build boundary

- Work on a feature branch and use a pull request. Never develop directly on
  `main` or on a live VM.
- Build and exercise changes in Test only.
- Never deploy to Production from Engineering / Build. Production promotion
  belongs to ROBIE — Release / Production and requires Carlo's explicit
  approval for the exact QA-certified digest.
- Preserve a rollback pointer before every Test change. Do not delete releases,
  job state, evidence, browser profiles, or open-job checkpoints.
- A green CI run, successful command, pointer flip, worker statement, or click
  is not business-outcome proof. Missing authoritative evidence is
  `UNVERIFIED`, never `COMPLETE`.
- Every meaningful bug fix requires a regression test.

## GCP access

- Use GitHub Actions Workload Identity Federation. Do not ask Carlo to preserve
  a browser login and do not create service-account JSON keys.
- Credential-bearing workflows must run from protected `main` only. Never
  grant `id-token: write` to pull-request code that can be changed by the PR.
- Use the audit identity for reads and the Test deployment identity only for an
  approved Test release workflow. Never give either identity Production write,
  Secret Manager payload, billing, or IAM-administration permissions.
- Authentication success proves only identity exchange. It does not prove the
  running application version. Follow `docs/GCP_ACCESS_RUNBOOK.md` and preserve
  the generated evidence.
- An LLM must have its own authorized access to this private GitHub repository.
  Access is inherited from GitHub workflows, not from Carlo's Google session.

## Browser automation

Prefer deterministic Playwright page objects, stable DOM selectors, explicit
state waits, post-action read-back, session-expiry handling, idempotent retries,
and durable checkpoints. Visual or model-driven clicking is a documented
fallback only. Prevent duplicate customer, EZLynx, email, payment, bind, and
carrier actions.

**EZLynx notes and documents are API only.** File notes through DiscussionApi
(`add_note_to_discussion` / `file_note_to_existing_discussion`) and upload
files through DocumentApi (`upload_applicant_document`). Playwright and CDP
are for forms and portals only — never Add Note, Save Note, or a file chooser
against EZLynx. COMPLETE is refused without a read-back `note_id` or
`document_id`. See `docs/EZLYNX_NOTES_DOCS_API_ONLY.md`.

## Required handoff

Every candidate handoff must identify the requirement, issue, branch, PR,
commit, Test release and digest, commands/tests and results, evidence locations,
remaining risks, rollback target, and exact QA scenarios. Hand the candidate to
ROBIE — Reliability / QA; do not self-certify it.
