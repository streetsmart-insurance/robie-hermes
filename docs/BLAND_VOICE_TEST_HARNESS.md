# Bland voice test harness - review draft

This branch contains **offline evaluation only**. It places no calls, has no
Bland credential, edits no customer record, and sends no messages. Synthetic
fixtures are invented; no client phone, transcript, address or payment data
is committed. There is no production deployment or scheduled test.

## Test design

1. Confirm a number controlled by the agency and consent to receive the test
   calls, with a call count and window. Never dial a client number or reuse a
   live refund transcript as a test fixture. Record the target and permission
   before any call. `validate_test_plan` does not establish ownership; it only
   rejects plans that do not explicitly attest it.
2. Use a set of invented street/city/state/ZIP cases including near-sounding
   words, apartment/unit numbers, spelled names, accents, noise and corrections.
   One scripted call per case. Record Bland call ID, telephony outcome, raw
   transcript, and independently reviewed audio if the person consents. Grade
   exact street/city/ZIP capture; do not autocorrect “Headon” to “Haddon” or
   “Dipsboro” to “Gibbsboro” from expectation alone.
3. Agent repeats the full address back and obtains an explicit “yes” or
   correction. A transcript's yes after the wrong repeat-back fails. Even a
   correct transcript is not safe for a customer write without audio review
   and the governed EZLynx identity/address workflow.
4. Separate dial accepted, call placed, actual human conversation, voicemail
   detected, confirmed message left, and no-contact outcomes. A call dying at
   voicemail detection is not outreach completed. Count unknowns, dropped
   calls and duplicate IDs in the denominator; never inflate the human rate.
5. Extract each consequential claim (“I updated your address,” “we issued a
   check,” “I left a message”) and independently read the destination record
   after the claim. Match exact target/action/value and source ID; transcript
   or Bland dispatch alone does not prove it. Mismatch and no-readback fail.

## Reliability loop

Test first with synthetic calls; run the owned-number set only after the number
and calling window are approved. Review a stratified audio sample, including
near-misses and errors. File a specific bug with call ID, expected vs heard,
repeat-back and actual source-system readback, without committing real data to
GitHub. Fix script/ASR or handoff logic in another draft PR, rerun the same
cases plus new counterexamples, and track false claims and false address
confirmations separately. No automatic promotion from a score. Carlo reviews
any policy or production change; merge and deploy require his distinct go.

The existing `audit_verification_worker.py` has a Bland dispatch adapter and
queue discipline; this harness does not change it or the live calling path.
