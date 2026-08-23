# ROBIE job recordings and confidence

Every claimed Computer Worker run can record the EZLynx browser tab from the
persistent Chrome session. The capture is tab-only: it does not record the Mac
desktop, Google Chat, Gmail, passwords, or unrelated tabs. Recording failures
are diagnostic failures and never stop the Job itself.

## Runtime controls

- `ROBIE_RECORD_ALL_JOBS=1` enables capture.
- `ROBIE_RECORDINGS_DRIVE_FOLDER_ID` is the StreetSmart Shared Drive folder.
- `ROBIE_GOOGLE_TOKEN_FILE` points to Robie's protected Workspace OAuth token;
  this is required when Shared Drive policy excludes Google Cloud service accounts.
- `ROBIE_BROWSER_CDP_URL` defaults to `http://127.0.0.1:9222`.
- `ROBIE_RECORDING_FPS` defaults to 4 to control size and CPU use.
- `ROBIE_DELETE_LOCAL_RECORDING_AFTER_UPLOAD=1` removes the VM copy only after
  Drive confirms the upload. The Drive copy is retained until a user deletes it.

Each recording stores its Job ID, segment, hash, size, final Job status, Drive
link, and any capture/upload failure. The Jobs ledger shows the latest recording
link, confidence, issues, and whether the clip is approved as a reference.

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
