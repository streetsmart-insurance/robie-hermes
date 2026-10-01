# Jake connection test, draft only

Separate from carrier campaigns. One exact-number exception for the reviewed
connection test, not a carrier-directory entry or lifted global guard.
No default voice. Caller ID must be verified owned by the Bland account.
One minute maximum, recording disabled, hang up on voicemail, no vendor retry,
redial, transfers, SMS, tools or webhooks. Uses exactly the reviewed opening,
follow-up and close. An AI prompt cannot guarantee exact generated words beyond
first_sentence; this limitation must be surfaced before live use.

The execute path is Test-host-only and approved-day-only. Durable private SQLite
claim commits before POST. Unknown transport outcomes or missing call IDs never
retry; check the vendor before deciding what happened. POST acceptance is not
proof Jake answered; GET readback must establish queue/progress and later outcome.
The fixed test ID must identify this exact owner approval, not be regenerated to
bypass the once-only gate.

The CLI defaults to a local payload preview. --execute reads only the staged Test
key version 1 through VM metadata/Secret Manager, which currently returns 403.
No IAM policy is changed by the runner. The actual voice/persona choice, caller-ID
ownership and account-specific cost remain preflight gates outside this draft.
Never run with --execute until those facts and the owner's exact scope match.

Private durable state and isolated Test code install are needed; no systemd
service, cron or gateway switch. No Production deployment. Synthetic tests use
mock HTTP and credentials only. No real recordings, keys, transcripts or logs
are committed. A dated factual result note must report accepted/queued/answered
accurately and never equate queued with successful conversation.
