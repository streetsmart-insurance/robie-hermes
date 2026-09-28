# Bland double-dial policy - draft PR #652 (review only)

Status: pushed as draft PR #652 on the private robie-hermes repo, for
review only. Nothing here is merged, deployed, or authorized to place
calls. The repo was flipped private on 2026-09-28 (verified). All
fixtures are synthetic; never commit real numbers, recordings,
transcripts, or logs.

## Policy `double-dial-v1`

- Exactly ONE redial per target, only when attempt 1 is conclusively
  classified `voicemail_no_message` from call-detail evidence. A queued or
  completed API status is never, on its own, evidence of a conversation.
- `answered_by=human` requires transcript + duration corroboration.
- The name-and-reason screener (iOS call screening, Google Call Screen) is
  a live-wait state (`screener_waiting`), never terminal and never a
  mailbox: answer name/reason immediately and directly (repeat if asked),
  then stay on the line silently. Never say goodbye or hang up unless told
  the person is unavailable or the call rolls to voicemail.
- Redial: same pinned caller ID (+17322986745), fires 10 seconds after the
  conclusive attempt-1 outcome, and must dispatch within 180 seconds of
  the FIRST dispatch (outer bound). A missed window records MISSED_WINDOW;
  a late dial is never placed.
- Voicemail per attempt: attempt 1 reaching voicemail (or screening with
  no pickup) hangs up with NO message. Attempt 2 reaching voicemail LEAVES
  the message: `build_attempt2_voicemail_script` - Eva identity, slow and
  clear, callback 732-481-2520.
- Identity on every call (first breath, before anything else; screeners
  cut in after ~2 seconds): "This is Eva, an AI assistant calling on
  behalf of Jake from StreetSmart Insurance, [reason]." AI disclosure
  stays in the first sentence. Pace: slow and measured; one or two
  sentences per response.
- No redial after human contact; no third call, ever.
- no_answer / busy are terminal by default; redial eligibility for each
  sits behind a config flag pending the owner's ruling.
- screener_declined (call ended, screener engaged, no human pickup) is
  redial-ELIGIBLE with the same 10s delay and 180s window. Configured by
  Jake (his 2026-09-28 iMessage: a first call going straight to voicemail
  or a screen with no pickup gets the automatic callback within 10
  seconds) within the voice-settings lane Carlo delegated to him; the
  config flag flips it without rework.
- Carriers only: only carrier-directory numbers in E.164 are dialable;
  client/insured/prospect/row-supplied numbers are refused, and a
  directory number matching a client phone on the row is refused.

## Source of these values

The identity, screener behavior, 10s retry delay, per-attempt voicemail
behavior, callback number, and pacing are Jake's CONFIGURED choices, from
his production-config recap and screener ruling sent 2026-09-28 over his
own iMessage. Carlo delegated the Bland voice-settings lane to Jake on
2026-09-28 ("Let that be Jake's lane. He can modify it."), covering
voice/persona, script, voicemail behavior, and retry timing - including
attempt-2 voicemail, screener-declined redial eligibility, and the 10s
clock. These are configured, tested settings, not open questions. Earlier
draft defaults (zero intentional delay, no voicemail on any attempt) are
superseded. Config flags remain so future changes stay easy.

## Architecture

- `robie_job_engine/bland_double_dial.py` - pure policy: outcomes,
  classifier, redial decision, target validation, script builders. No I/O,
  no secrets, no clock.
- `robie_job_engine/double_dial_worker.py` - durable worker, polling-first
  (bounded call-detail polling, capped backoff, one idempotency key per
  attempt, status re-read immediately before dispatching the redial).
  sqlite: attempts PK (policy_version, target, attempt_seq) with UNIQUE
  call_id; the redial is an INSERT-once row committed BEFORE dispatch;
  events deduped by content fingerprint. A future webhook receiver feeds
  the same record_outcome/maybe_redial interface.
- `tests/test_bland_double_dial.py` - 18 tests, all fixtures synthetic.

## Carlo's remaining separate decisions

1. Target audience (carriers only vs anything else).
2. Test-scoped Bland secret name on hermes-test-01 (never the Production
   secret).
3. First live test (recipient, count/window, retention; Jake's consent
   for his number).
4. Merge, deploy, and any production config change.
Resolved 2026-09-28: repo privacy (flipped private, verified), the
draft-PR push (#652, review-only), and the voice-settings lane
(voice/persona, script, voicemail behavior, retry timing) delegated to
Jake - the voice settings above are his configured, tested choices, not
open questions.
