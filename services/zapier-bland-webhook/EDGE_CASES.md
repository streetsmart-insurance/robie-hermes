# Edge Cases — Robie Voice Calling System

**Date:** 2026-10-03
**Scope:** `bland-dispatcher/` (Cloud Run, 10 deterministic label Zaps) and
`robie_job_engine/robie_call_handler.py` (freeform task handler)

Carlo's directive: *"Think of all the edge cases and how sloppy humans are,
and we need to build trust. If something comes back that's not in their
regular workflow, they will doubt you."*

Every case below is implemented with a regression test. Cases deliberately
NOT handled are listed at the bottom with reasoning.

---

## 1. Never claim a call happened when it didn't

**The September incident:** false "call dispatched" notes were written for
calls that never happened. This is the trust-killer.

**Fix (both codebases):** Success is now defined as *verifiable connection*,
not *API accepted the request*. Each attempt is classified:
- `connected` — Bland's `final_status` shows `answered_by: human|voicemail`,
  or a completed call with duration > 0
- `failed` — POST rejected (4xx), carrier failure, no-answer, busy, canceled
- `unknown` — POST accepted but status unconfirmed (timeout, poll timeout,
  missing final_status)

The note says "The call was successful." **only** for `connected`.
For `unknown`, the note says plainly: *"We couldn't confirm whether the
call went through."* It never claims failure as fact either — because the
call may have happened.

**Tests:** `test_edge_cases.py` (dispatcher), `TestOutcomeNoteHonesty` (handler)

---

## 2. Timeout after Bland accepted the call (double-dial hole)

**Scenario:** `POST /v1/calls` times out, but Bland already placed the call.
Without a guard, the next attempt (or 30-min redelivery) dials the client
a second time.

**Fix (dispatcher):** On timeout/network failure, query Bland's recent calls
for the number (`BlandClient.recent_calls`). If a call went out in the last
5 minutes, the result is flagged `possible_call_placed` and the note
reports uncertainty instead of a false failure.

**Fix (handler):** Before dialing, check `ports.bland.recent_calls(phone)`.
If a call to this number exists within 30 minutes, **do not dial**. File a
note explaining why (*"Robie did NOT dial again to avoid calling twice"*)
and leave the task for human review.

**Tests:** `test_timeout_with_recent_call_marks_possible`,
`TestRecentCallGuard`

---

## 3. Disconnected first number, good second number

**Scenario:** Applicant has CellPhone (disconnected) and HomePhone (good).
Old code dialed only the first and gave up.

**Fix (dispatcher):** Try phones in order. A "bad number" failure
(disconnected, invalid, not in service, unreachable) moves to the next
number. A *service-level* failure (Bland 503, timeout) stops immediately —
don't burn the client's other numbers on a broken service. The note tells
staff: *"The first number on file was disconnected, so we tried the next
number on file."*

**Tests:** `test_bad_number_fails_over_to_next_phone`,
`test_service_failure_does_not_burn_other_numbers`,
`test_all_phones_bad_reports_clearly`

---

## 4. Kill switch flipped mid-dispatch

**Scenario:** Carlo hits the kill switch during a call. The 10-second
redial wait elapses, and the second dial goes out anyway.

**Fix:** `call_with_double_dial` accepts a `halt_check` callable. After the
redial wait and before placing attempt 2, if the halt is active, the redial
is skipped with `halted_before_redial: True`. The note says: *"The first
attempt went to voicemail. The callback was not placed because the system
was paused mid-call."*

**Tests:** `test_halt_check_stops_redial`, `test_halted_redial_note_is_plain_english`

---

## 5. Bland API down — fail fast

**Scenario:** Bland is down. Old code did EZLynx lookups (burning API quota)
before discovering Bland was unreachable, producing a confusing downstream
error.

**Fix:** `BlandClient.is_available()` exposes the circuit-breaker state.
`dispatch()` checks it **before** any EZLynx lookups and fails fast with:
*"Bland calling service temporarily unavailable (circuit breaker open)"*.

**Tests:** `test_circuit_open_fails_fast_before_lookups`

---

## 6. Recording download fails or returns garbage

**Scenario:** Bland returns a `recording_url`, but the MP3 download fails,
returns empty, or returns an HTML error page. Old code reported "pending"
(not yet available) — staff would wait forever for a recording that will
never arrive.

**Fix:** `_looks_like_audio()` validates size (>1KB) and header (ID3/MP3
frame sync/RIFF). Failed/empty downloads are reported as `failed`, not
`pending`. The note says: *"The call recording is not available — the
upload failed."*

**Tests:** `test_tiny_mp3_rejected`, `test_valid_mp3_headers_accepted`,
`test_failed_download_reports_failed_not_pending`

---

## 7. "Recall the policy" is not a call task

**Scenario (handler):** Substring keyword matching meant "Recall the policy"
(containing "call"), "morning meeting" (containing "ring"), and "telephone
log" (containing "phone") were all classified as call tasks. A mistaken
classification could dial a client for a non-call task.

**Fix:** Word-boundary regex (`\b(call|phone|dial|ring|callback|call\s+back)\b`).
Only whole words match.

**Tests:** `TestWordBoundaryKeywords` (5 tests)

---

## 8. "Call him" — ambiguous instructions

**Scenario (handler):** Staff writes "call him" with no topic. Old code
would pass this to Eva, who would have to guess.

**Fix (Carlo's rule):** If the instruction, after stripping call verbs,
has no topic signal (about/regarding/renewal/payment/etc.) and is very
short or just pronouns, the task is **not dialed**. A clarification note
is filed: *"Robie received a call task but couldn't act on it… Please
update the task with what the call is about."* The task stays open.

**Tests:** `TestAmbiguityGuard` (8 tests)

---

## 9. Task names a different phone number than EZLynx

**Scenario (handler):** Task says "Call John at 555-999-8888" but EZLynx
has a different number. Which wins?

**Fix:** EZLynx wins (system of record). The mismatch is flagged in the
note: *"The task mentioned a different phone number than the one on file;
we called the number on file."* The wrong number is never written to the
note (the Discussion API rejects phone numbers — it's scrubbed).

**Tests:** `TestPhoneMismatch` (4 tests)

---

## 10. Task names a different person than the applicant

**Scenario (handler):** Task says "Call Mary Smith" but the applicant is
John Test. Wrong applicant selected, or a sloppy copy-paste.

**Fix:** Detect `Call <FirstName> <LastName>` patterns and compare to the
applicant name. On mismatch, still dial (the applicant_id is the source of
truth for the number), but flag it: *"The task mentioned Mary Smith, but
the applicant on file is John Test — please check the right person was
reached."*

**Tests:** `TestNameMismatch` (4 tests)

---

## 11. Same instruction assigned twice (different task IDs)

**Scenario (handler):** Sloppy human assigns "Call about renewal" as two
separate tasks. Task-ID idempotency alone would dial twice.

**Fix:** Content-based dedup on `(applicant_id, normalized_instruction)`
within 30 minutes. Normalization is case/punctuation-insensitive, so
"CALL ABOUT THE RENEWAL" and "Call about the renewal!" dedup. The second
task is suppressed with `duplicate_reason` set.

**Tests:** `TestContentDedup` (4 tests)

---

## 12. Task already closed (stale CSV)

**Scenario (handler):** The CSV is generated up to 30 minutes before
processing. A human may have closed the task since. Calling on a closed
task erodes trust.

**Fix:** Optional `TaskStatusPort.is_task_open(task_id)`. If the task is
closed, skip with `skipped_closed_task: True`. If the port isn't wired or
the check fails, proceed (graceful degradation — the port is best-effort).

**Tests:** `TestStaleCsvGuard` (4 tests)

---

## 13. ALL CAPS instructions

**Scenario (handler):** Staff writes "PLEASE CALL JOHN ABOUT HIS RENEWAL"
in all caps. TTS would shout or spell out words.

**Fix:** `_normalize_spoken()` collapses whitespace and converts
>80%-uppercase text to sentence case for `first_sentence` and
`voicemail_message` (the spoken parts). Eva's task prompt keeps the
original verbatim (she's an LLM and handles it fine).

**Tests:** `TestSpokenNormalization` (4 tests)

---

## 14. Voicemail detection uncertainty

**Scenario (both):** Bland returns `answered_by: "unknown"`. Old code
treated this as a successful human answer.

**Fix:** The note states the uncertainty plainly: *"The call connected but
we couldn't confirm whether it reached the person or voicemail."* The
double-dial does **not** redial on `unknown` — if it was a human who hung
up quickly, redialing would be annoying. The human reading the note can
decide.

**Tests:** `test_unknown_answered_by_note_states_uncertainty`,
`test_attempt_outcome_unknown_*`

---

## 15. Writeback failure after successful call

**Scenario (both):** The call happened but the EZLynx note didn't land.
Silent data loss — the worst outcome.

**Fix (already existed, verified):** Immediate chat alert: *"Bland call
COMPLETED but EZLynx writeback FAILED… Manual note may be needed."* The
task is **not** reassigned (handler) so a human sees the incomplete record.

**Tests:** `TestWriteback::test_writeback_failure_alerts_immediately`

---

## Deliberately NOT handled

### Webhook authentication
The `/webhook` endpoint is unauthenticated. Adding a shared-secret check
requires reconfiguring all 11 Zapier Zaps. Noted as a security gap;
doesn't affect call reliability. Recommend addressing separately.

### Durable idempotency across restarts
The idempotency maps (`_seen`, `_processed_tasks`, `_processed_content`)
are in-memory per process. A Cloud Run instance restart or worker restart
loses the 30-minute window. The EZLynx Discussion API doesn't return note
bodies, so we can't durably check "did we already write about this task."
Mitigation: the 30-minute CSV cadence and Zapier's at-most-once delivery
make the window small. A Redis/Firestore-backed store would close this
fully.

### Wrong-person-answered detection
If Eva reaches the wrong person and they don't say "wrong number" clearly,
we can't reliably detect it from `answered_by` alone. The transcript
summary (when available) is included in the note verbatim so staff can
spot it. LLM-based transcript analysis would be the next step.

### Concurrent worker runs on the same CSV
If two worker processes ingest the same CSV simultaneously, both could
dial before either marks the task processed. The 30-minute single-schedule
design makes this unlikely; a distributed lock would be needed for a
multi-worker setup.

### Bland API `recent_calls` availability
The `recent_calls` guard depends on Bland's `GET /v1/calls` list endpoint.
If Bland changes or rate-limits it, the guard degrades gracefully
(`ok: False` → proceed with normal idempotency). The timeout hole is
then only partially covered.

---

## Test summary

| Suite | Tests | Status |
|-------|-------|--------|
| `bland-dispatcher/tests/` (incl. `test_edge_cases.py`) | 119 | ✅ pass |
| `robie-hermes/tests/test_robie_call_handler.py` | 36 | ✅ pass |
| `robie-hermes/tests/test_robie_call_edge_cases.py` | 39 | ✅ pass |
