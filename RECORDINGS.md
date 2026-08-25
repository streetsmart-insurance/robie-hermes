# ROBIE job recordings and confidence

Every executable Computer Worker attempt must record the EZLynx browser tab from the
persistent Chrome session. The capture is tab-only: it does not record the Mac
desktop, Google Chat, Gmail, passwords, or unrelated tabs. Recording failures
fail closed. If required capture cannot enter `RECORDING`, the Job becomes
`FAILED` before the worker runs. Independent verification occurs while capture
is still active. `COMPLETE` is not authorized until every numbered segment is
uploaded to Drive and has a stored link.

## Runtime controls

- `ROBIE_RECORD_ALL_JOBS=1` enables capture.
- `ROBIE_RECORDINGS_DRIVE_FOLDER_ID` is the StreetSmart Shared Drive folder.
- `ROBIE_GOOGLE_TOKEN_FILE` points to Robie's protected Workspace OAuth token;
  this is required when Shared Drive policy excludes Google Cloud service accounts.
- `ROBIE_BROWSER_CDP_URL` defaults to `http://127.0.0.1:9222`.
- `ROBIE_PLAYWRIGHT_CDP_URL` points the approved deterministic browser runner
  at the same persistent Chrome session.
- `ROBIE_RECORDING_FPS` defaults to 4 to control size and CPU use.
- `ROBIE_DELETE_LOCAL_RECORDING_AFTER_UPLOAD=1` removes the VM copy only after
  Drive confirms the upload. The Drive copy is retained until a user deletes it.

Executable Skills are registered in `robie_job_engine/job_schema.py`. Each
contract declares the expected destination result, recording policy,
independent verifier, maximum attempts, success conditions, and failure
conditions. Missing or invalid contracts prevent execution.

Each recording stores its Job ID, numbered segment, hash, size, final Job status,
Drive link, upload state, failure stage, and any capture/upload failure. The Jobs
ledger writes every segment link into the corresponding Recording cell. It shows
`Recording failed` or `Recording upload failed` instead of silently leaving the
cell blank.

Recorder startup uses a first-frame readiness handshake. Launching the capture
process is not enough: the Job Engine does not permit executable work until
Playwright has attached to the selected tab and delivered the first frame to the
video encoder. Attach failures, early exits, and readiness timeouts are stored
as `failure_stage=START` and fail the Job before the worker can change anything.

The approved final statuses are `COMPLETE`, `FAILED`, `UNVERIFIED`, and
`NEEDS_AUTH`. Authentication, MFA, CAPTCHA, or other human-login intervention
must use `NEEDS_AUTH`; it never implies success.

## Confidence

Confidence is evidence-based. It does not use Gemini's self-reported confidence.
A Job can receive `HIGH — 98%` only when its final state is COMPLETE and the Job
Engine has stored verified, authoritative destination evidence. UNVERIFIED and
FAILED Jobs remain low-confidence and show the concrete issue.

## Videos are not automatic training data

Raw videos can contain customer information and mistakes. They are never placed
into ROBIE's reference or training set automatically. A clip must be reviewed,
redacted, documented, and explicitly approved for reference. Training approval
is a separate second gate. Approved clips can improve Skills, regression tests,
or a future supervised evaluation set; they do not cause Gemini to retrain itself.

Administrative review commands:

```text
python -m robie_job_engine.recording_admin --db /path/to/jobs.db approve-reference RECORDING_ID --reviewer "Name" --notes "PII removed; demonstrates correct upload" --redacted
python -m robie_job_engine.recording_admin --db /path/to/jobs.db approve-training RECORDING_ID --reviewer "Name"
python -m robie_job_engine.recording_admin --db /path/to/jobs.db manifest
```
