# Bounded document upload reliability

This candidate repairs the narrow single-document workflow. It does not certify
general quoting, notes, policy association, or multi-step message completion.

## Supported pilot request

Use subject `Document upload` (or `Task` / `Robie task`) and exactly this body:

```
Upload document "test.txt" to applicant 220250093.
```

Attach exactly one file named `test.txt`. The same standalone sentence is
recognized in Chat; Chat still uses the document tool to select its local
source file. Email matches the original attachment name and executes the API
directly, without the generic agent/browser loop. Extra requirements, arbitrary
email subjects, multiple attachments, and policy associations are not silently
treated as this bounded task. They retain their existing route or are held.

## Execution and proof

- The document tool journals an intent in the existing JobStore before POST.
- The key includes account, display name, and policy master ID. A changed source
  fingerprint for the same intent is refused. No new duplicate ledger is used.
- The returned numeric ID is persisted before any read-back. Subsequent calls
  with that ID perform reads only; simultaneous calls cannot both own the POST.
- A missing response after POST leaves an uncertain intent. It refuses another
  upload and requires reconciliation; it does not infer absence from a partial
  document search. Automatic reconciliation of that ambiguous case is not in
  this candidate.
- The document must be found by ID and exact name in a fresh account search,
  and its downloaded bytes must match the source SHA-256.
- A separate message verifier repeats account/ID/name/content verification.
  It does not require a policy for an applicant-level document.
- Full completion is enabled only for the entire bounded request. A document
  receipt cannot satisfy an additional note, task, quote, or policy change.
- Upload exceptions retain a redacted diagnostic checkpoint with saved state,
  failure class, known document ID, and READ_BACK_ONLY or RECONCILE_BEFORE_WRITE.
  Diagnostic persistence failure must not replace the underlying exception.

Deduplication is within the same durable job and operation. Independent requests
with different job IDs are not business-deduplicated by this patch. Standalone
calls with no job context have content read-back but no durable retry protection;
partial or conflicting job context is refused. The fingerprint proves equality
to the selected source bytes, not the business correctness of that source.

The existing DocumentApi search may return a limited page. Failure to find the
returned ID remains unverified rather than authorizing another POST. Add supported
pagination or account-bound lookup once its actual endpoint contract is verified.

## Validation and release handoff

- Base: `6e5342a3241843506e21c19bd605b65604e88e27`.
- Feature branch: `fix/document-workflow-reliability`.
- Requirement: September 19 job `802682ec` saved a document but timed out and
  failed policy-based verification; synthetic probes also exposed insufficient
  source-content verification.
- Focused suite: `PYTHONPATH=.:tests python -m pytest -q tests/test_document_upload_reliability.py tests/test_ezlynx_document_tool.py tests/test_chat_ezlynx_destination_verifier.py tests/test_email_guard.py tests/test_chat_verifier_routing.py tests/test_chat_verifier_wiring.py`.
- Named regression: `document-upload:content-and-retry`. Incident remains open.
- Fresh baseline readiness, September 20: Test run `35537100854`, Production
  run `35537102447`; both reported release `6e5342a32418` and seven passing
  readiness checks. This is infrastructure evidence, not document acceptance.
- Full local baseline and candidate suites both initially reported 34 failures
  and 11 errors, including sandbox-denied home-cache test fixtures and platform
  assumptions. Official Linux CI must be evaluated separately; do not waive it.

Before promotion: review the exact candidate, build an immutable artifact,
inventory Test jobs and preserve rollback, install via the approved protected-main
Test workflow, then exercise synthetic upload plus interrupted-save/read-back
scenarios. Record exact release digest, timings, destination proof and repeated
request results. Test must use the designated test account, even if its API host
is shared with Production. The only presently verified rollback baseline is the
reported running release above; retrieve and verify its archive digest before
any deployment. No candidate deployment or live business write is claimed here.

Remaining separate work: full September 19 call timeline, general stuck-agent
detection, note/task partial completion, cross-job deduplication, quote workflow
acceptance, and a measured reliability scorecard. Owners: Carlo and Jake;
Release / Production promotes only the reviewed, QA-certified digest.
