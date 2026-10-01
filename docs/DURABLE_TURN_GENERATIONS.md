# Durable turn identity — local correctness fix

This follows local combined candidate `f535b332282a7c3d001e0e08f412081bcd036d81`.
The original counter restarted at 1 after finish. A second clarification/execution
turn could therefore accept an earlier turn's confirmed-write checkpoint.

Every gateway generation now receives a UUID, persisted as `model_generation` in
the existing JobStore checkpoint table before worker execution. SQLite transactions
serialize current-generation replacement and receipt comparison/write. No new job
executor, credential, external API, approval or write permission is introduced.

The gateway persists off its event loop, then binds the returned identity to its
context. Async tasks and `asyncio.to_thread` carry that identity; the listener's
context is restored after dispatch. Its watchdog checks the captured generation,
not just job ID. A delayed start cannot bind a superseded token. Late model sends
are refused before reply finalization; fallback close checks its captured token.
The note receipt hook prefers the retained context job over a mutable global job
variable. An uncorrelated callback cannot manufacture confirmation authority.

`turn_write_log` is accepted only when its nonempty note ID and exact generation
match both the caller and current durable generation. The write must still pass
the existing independent readback before the receipt hook records it. Missing,
legacy numeric, or superseded generation receipts fail closed. They are not
migrated or treated as permission to repeat an external write.

Finishing clears the caller identity and signals only its own generation. A late
callback cannot finish a newer generation. Same-generation receipt retries are
idempotent. After finish/restart, `resume_model_generation(store, job, token)` can
bind an explicitly retained matching token for receipt/reply recovery; it does not
run the model, tools or any business write. It never guesses the latest token for
an unknown caller. A new execution calls begin and gets a new identity instead.
Existing reply-outbox request/message IDs still deduplicate transport recovery.

Engine duplicate/clarification questions carry their generation. An earlier
question cannot suppress a later generation's reply. Same-generation duplicates
remain suppressed. Existing confirmation, scope, hard-wall, destination-readback
and reply-outbox policies are unchanged. Confirmation alone is not a write receipt.

Store-less begin calls remain for old synthetic fixtures but cannot authorize a
write receipt. Runtime gateway starts always supply the real JobStore. A boundary
that loses context must retain/bind the exact token explicitly; without it, write
claims are refused. Tests prove Python task/thread propagation and explicit
separate-process recovery, not a deployed Hermes integration.

Regression coverage includes successive-turn counterexample; exact same-generation
retry; separate-process restart; stale and missing receipt IDs; concurrent starts;
late write/completion; captured fallback identity; context cleanup and mutable-env
isolation; old question/approval checkpoints; fail-before-start on storage error;
and repeated outbox recovery with no second business write or delivered message.

No live Test acceptance occurred. The candidate requires the normal exact-artifact
QA, actual runtime inventory and separately authorized Test driver IN. Deployment,
publication, shared merges and Production GO remain unapproved. Native-task and
DiscussionApi WIP stay excluded. Preserve the original candidate and durable job
state for rollback; old code cannot safely interpret the new string epoch as its
legacy integer counter, so a rollback needs controlled process shutdown and review
of pending generations rather than replaying uncertain work.
