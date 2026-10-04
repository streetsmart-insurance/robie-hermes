# Recover undelivered Chat replies

Local follow-up to draft PR #701 at
`f0ddc34815efe823c21485635d3de82688f0ba0d`. Originally reviewed on `a39bd4e`,
then rebased cleanly onto the account-name matching fix. No deployment or QA
certification.

After a Google Chat 429 exhausted the immediate retries, the old adapter kept
the retry text in memory. The failure checkpoint survived a gateway restart,
but no retry was restored. Even without a restart, recovery needed another
successful send for the same job.

## Behavior

`send()` now commits the complete, formatted text reply and its original thread
to `chat_reply_outbox` in the existing Job database before the first HTTP call.
This covers job-owned text replies with a thread, excluding transient busy
notices. It does not add another job executor.

- Every chunk has stable Google `requestId` and `messageId` values. The same
  values and body survive retries, competing drainers and process restarts.
- A 409 requires a matching message read-back; collision alone is not success.
- Named threads use `REPLY_MESSAGE_OR_FAIL`, so a missing thread cannot silently
  redirect a recovered reply to a new one. First replies retain their stable
  job thread key.
- Each accepted chunk is recorded. A crash midway through a reply resumes at
  its first unrecorded chunk. If Chat accepted that chunk before the crash,
  server idempotency protects the retry.
- The final receipt and delivered state commit together. A receipt-write
  failure leaves a recoverable lease, rather than turning server acceptance
  into permission to create a second message.
- A background task starts on adapter connection, checks up to ten due replies
  per pass, and stops on disconnect. Database work runs off the gateway loop.
  A successful foreground send may also wake due replies for that job.
- Retry state is durable: five delivery rounds maximum, 30/60/120/240-second
  backoff, a 180-second lease, and a two-hour age limit. Each round uses the
  existing three-attempt transient HTTP retry policy. Permanent failures and
  exhausted work remain recorded as failed; nothing claims delivery without
  a server message name.
- A stop suppresses an old outcome/question, while an explicit stop notice can
  still be delivered. A new clarification answer supersedes an old reply.
  Questions for terminal jobs are suppressed. Normal completed outcomes may
  still recover.

Recovery calls only the Chat transport. It does not call the model, the
response verifier, `_finish_sent_reply`, the Job Engine executor, or EZLynx.
It does not promote a job's status or convert a delivery receipt into business
completion evidence.

API contract checked against Google's
[message creation documentation](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/create):
request IDs deduplicate identical requests, custom message IDs are unique
within a space, and strict thread replies fail when the target cannot be used.

## Validation

Use Python 3.12 with the repository CI dependencies. All new tests use fake
Chat APIs and isolated synthetic SQLite databases.

```sh
NO_GCE_CHECK=true ROBIE_ENV=TEST PYTHONPATH=.:tests python -m pytest -q \
  tests/test_chat_reply_recovery.py tests/test_round*.py \
  tests/test_reply_and_go_guards.py
NO_GCE_CHECK=true ROBIE_ENV=TEST PYTHONPATH=.:tests python -m pytest -q tests
NO_GCE_CHECK=true ROBIE_ENV=TEST PYTHONPATH=.:tests python -m robie_job_engine.regression_battery --ci
```

The new tests cover exhausted 429 then a fresh adapter; autonomous recovery;
success persistence; repeated sends and repeated recovery; concurrent leases;
server acceptance followed by timeout; cancellation before receipt commit;
receipt storage failure; 409 matching/mismatching read-back; interrupted chunks;
stop notices; stale and parked questions; permanent versus transient errors;
durable attempt/age limits; wrong-space rejection; formatting; and shutdown.

The full suite must be compared with the exact untouched base. Existing
allowlist, ephemeral-store, readonly-home fixture, and process-cleanup failures
are not waived by this patch. Passing focused tests is not release readiness.

## Limits and handoff

Legacy `chat_delivery_failed` checkpoints lack a frozen wire envelope and stable
server IDs. They are retained but are not automatically migrated or resent:
guessing their original route or whether a message landed could duplicate or
misroute a reply. Recovery applies to replies recorded by this implementation.

Cards, attachments, idle notices, and separate direct-post helpers are outside
this change. An HTTP call already in flight when a stop arrives cannot be
unsent. Transport recovery does not resolve a job failed by startup orphan
classification. Long-pending/permanent failures need operator review after the
bounded recovery budget expires. Google-side idempotency and permissions still
need validation on the exact isolated Test release.

The new table is additive. A rollback preserves it and all existing checkpoints;
old code will not drain its pending rows. Do not delete those rows or replay the
underlying business action as a recovery method.

Before publication, compare with the current #701 head and integrate this local
commit into that existing work. Run its checks, then hand the exact candidate
to Reliability / QA. Test installation, live validation and Production
promotion require their separate authorization and immutable release evidence.
