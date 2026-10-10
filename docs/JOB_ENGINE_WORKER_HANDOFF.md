# Handoff: put a worker fully on the Job Engine with independent verification

Give this to any agent that builds or finishes a Robie worker (manual
renewals, audits, policy changes, carrier pulls, filing). Approved by Carlo on
2026-10-10. Follow `AGENTS.md` first: feature branch and pull request, Test
before Production, and only Carlo approves a Production deploy.

## The rule this exists for

A worker does the work. A separate verifier proves it, by reading the
destination again through the API. The Job Engine marks a job COMPLETE only
with that authoritative evidence. A note Robie wrote, a screenshot, an HTTP
200, or the worker's own report is never proof. Missing or ambiguous proof is
`UNVERIFIED`. A step that was refused or failed is `FAILED`.

## Shared write jobs (use these, do not write EZLynx directly)

| Job type | Done means | Status |
|---|---|---|
| `ezlynx.document_upload` | The exact document is on that applicant (fresh DocumentApi search, every page). | Being built (item 1). |
| `ezlynx.note_append` | Exactly one new note with exactly that text is in that discussion. | Being built (item 1). |

Until they land, a worker that has to file uses `upload_document_via_api` and
`file_note_to_existing_discussion`, which already read back. Do not call
DocumentApi or DiscussionApi POSTs yourself.

## Steps

1. **Contract.** One sentence for what "done" means in EZLynx or the carrier
   record, added to `robie_job_engine/job_schema.py` with the verifier class
   named. Example: "the renewal dec for policy X, term Y, is filed on the
   client and its premium matches the carrier's".
2. **Identity and no duplicates.** The job's idempotency key, for example
   policy number + term + document type. A rerun finds the existing job and
   never starts a second one.
3. **Steps.** List every external action: portal read, email, call, upload,
   note. EZLynx writes go through the shared jobs above. Email and calls keep
   their existing guards (carrier-only, business hours, never dial clients).
4. **Verifier.** Its own fresh read of the destination, never the worker's
   report. Record expected and observed. Missing or ambiguous is
   `UNVERIFIED`; refused or failed is `FAILED`. Never COMPLETE from a note,
   a screenshot, or an HTTP 200.
5. **Comparison.** When the job brings something back (renewal terms, audit
   papers, an endorsement), compare it with what was asked for: premium,
   term, limits, scheduled vehicles or equipment, loss payees, additional
   insureds. Use `robie_job_engine/robie_filer_extract.py` on the host
   (`pdftotext`, OCR for scans). A mismatch goes to a person, not to COMPLETE.
6. **Tests.** Regression tests with fakes for each outcome: done, unverified,
   failed, rerun (no duplicate), partial read, wrong client refused. Add the
   test files to `.github/workflows/ci.yml` by name: the required CI job runs
   a fixed list, so a file that is not listed never runs there.
7. **Test proofs.** Three clean jobs on `hermes-test-01` in a Moe window,
   recorded in `deploy/job_type_gate/promotions/<job_type>.json`. Re-run the
   eight standard QA checks after the Test install; each `check_evidence`
   entry carries the release `commit` and a `captured_at` between the install
   and QA sign-off (`RELEASE_PROCESS.md`).
8. **Handoff back.** Requirement, PR, commit, Test release and digest, tests
   and results, evidence locations, remaining risks, rollback target, and
   exact QA scenarios. Hand it to Reliability / QA. Do not self-certify.

## Do not touch without Carlo's explicit approval

- `robie_job_engine/ezlynx_write_scope.py` (which clients may receive a write).
- `robie_job_engine/release_promotion.py` and the release workflows.
- Anything that dials, emails, or writes for a real client outside the
  approved worker and its gates.

## Where each worker stands (2026-10-10)

| Worker | Status | What is left |
|---|---|---|
| Manual renewals (`manual_renewal_verification`) | 3 clean Test jobs recorded | Steps 4 and 5: verify what came back and compare terms; file through the shared upload job. |
| Audits (`audit_verification`) | 3 clean Test jobs recorded | Same as renewals, for audit papers. |
| Policy changes (`policy_change_verification`) | Off in code (`POLICY_CHANGE_ENABLED = False`) | All steps, then 3 clean Test jobs. |
| robie-filer | Live on Production for test client 220250093 only | Move uploads and notes to the shared jobs (after item 1). |
| `ezlynx-api` command line writes | Read-back and audit log, outside the Job Engine | Becomes the shared jobs (item 1). |
