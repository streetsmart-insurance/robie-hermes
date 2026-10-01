# Label and lead runner: Test replay

Routes Robie Call, Robie lead follow-up and the listed Robie client-outreach
org labels through phone_controls. Outreach takes priority over lead, then
Call. Multiple different outreach labels are held for review. Bare agency
labels and instructions in source-note prose do not dispatch or approve.

The reviewed Plan must bind applicant, discussion, source note, route,
recipient, script, voicemail text and caller identity. All client routes
require a one-call reviewed client exception. Freshness is checked, and
repeat source identity uses the persistent consumed campaign claim.

Run isolated replay on hermes-test-01:
python3 -m robie_job_engine.phone_test_replay --state-dir <new-private-directory>

Replay dispatches use reserved synthetic numbers and SYN call IDs. Four
routes exercise preparation, approval gate, dispatch claim, completion note
readback and duplicate suppression. Live calls and real notes remain zero.
This is connected replay, not connected live dispatch.

Runtime port work includes signed Test approval verification (no mint API),
verified directory record/freshness checks, fail-closed source-card schema,
and a live-effects-disabled port. The signed bridge must itself authenticate
an owner message; a signed string cannot replace owner evidence review.
No signing key is provisioned here. Fixture keys are synthetic only.

Operational blockers remain: current Test OAuth discussion reads do not
prove the old portal-card labels schema and swallow API errors into empty
lists. A portal-card reader must be verified or a new schema mapped from
real reads. No arbitrary fallback schema or successful-empty claim is used.
The old Production label dispatcher is not changed. Voice/caller identity,
account cost, exact speech and live DiscussionApi note adapter are unproved.
Do not enable the Production label watch or live flags based on this replay.
