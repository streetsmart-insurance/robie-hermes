# EZLynx Policy Setup v0.1.0-draft — Reliability / QA handoff

## Requirement

Install one Test-only `ezlynx-policy-setup` skill with 12 internal LOB
profiles. Missing, conflicting, guarded, or unsupported inputs must fail closed.
No real-client transaction, bind, payment, email, Production deployment, or
unverified `COMPLETE` is authorized.

## Candidate scope

- Profile manifest and expected-versus-actual contracts for all 12 requested
  profiles.
- Stable duplicate identity: applicant + carrier + policy number + effective
  and expiration dates + LOB.
- Required checkpoint contract: duplicate check, pre-Save snapshot, post-Save
  readback, and reopen verification.
- Reusable-evidence redaction.
- Production action-gate classification even when a generic Chat job or the
  grandfathered commercial-auto job type attempts to hide Policy Setup.
- Atomic Test skill installation and rollback evidence.
- Legacy Policy Setup multi-save orchestrator locked before its first browser
  action.

All profiles remain `Testing` with `consequential_writes_enabled=false`.
Browser duplicate search, checkpoint persistence, per-LOB page objects, and
reopen readback are not yet wired and therefore cannot be certified by this
candidate.

## Fixture approval

Reliability/QA must record sanitized Test fixture identifiers here before any
browser exercise. No fixture is approved by this engineering candidate.

| Fixture purpose | Applicant ID | Policy/record ID | Approval evidence |
|---|---|---|---|
| Duplicate absent | TBD | TBD | TBD |
| Exact duplicate present | TBD | TBD | TBD |
| Guarded/ambiguous input | TBD | TBD | TBD |
| Reopen verification | TBD | TBD | TBD |

## Exact QA scenarios

1. **Package/state:** On `hermes-test-01`, prove the installed skill symlink
   targets the immutable Test release. Verify version `0.1.0-draft`, all 12
   profiles `Testing`, `production_ready=false`, and all consequential-write
   flags false.
2. **Unsupported LOB:** Submit an otherwise complete sanitized intake for an
   unlisted LOB. Expect `NEEDS_SKILL`, no browser write, and no `COMPLETE`.
3. **Contractors/trucking:** Submit Commercial Auto — Contractors with trucking
   business use. Expect `NEEDS_SKILL` before browser navigation.
4. **Bonds:** Employee Dishonesty and Home Improvement Bond may reach `READY`
   with complete sanitized intake; Bid Bond, Performance Bond, missing, and
   unlisted variants must return `NEEDS_SKILL`. No result authorizes Save.
5. **Guarded rules:** Exercise each unresolved rule: Dwelling Fire
   mailing/location, Homeowners Full Value, silent GL aggregate, GL “if any,”
   Garage & Dealers mapping, and BOP placement. Expect
   `NEEDS_CLARIFICATION` and zero writes.
6. **Duplicate identity:** Prove whitespace/case normalization does not change
   the key, while changing applicant, carrier, policy number, either term date,
   or LOB changes it. With the approved exact-duplicate fixture, verify the
   eventual browser implementation refuses a second action.
7. **Legacy write regression:** Invoke `setup_policy_by_lob` with a mocked page.
   Expect `draft_write_gate`, `NEEDS_CLARIFICATION`, and zero locator calls.
8. **Generic Chat Production gate (isolated test):** Classify a generic Chat
   payload naming `ezlynx-policy-setup` under `ROBIE_ENV=PRODUCTION`. Expect
   `ACTION_GATE_REFUSED` before worker/Playwright start. Do not execute against
   Production.
9. **Expected versus actual:** Compare a matching reopened payload and a
   one-field mismatch. Expect PASS/FAIL respectively; both must include
   `authorizes_complete=false` in this draft.
10. **Evidence redaction:** Supply synthetic PII fields (name, address, DOB,
    license, VIN, email, phone). Verify reusable structured evidence redacts
    them while retaining non-PII coverage fields.
11. **Rollback drill:** In an isolated installer test, prove a failed post-flip
    verification restores both release pointers and the prior Policy Setup
    skill symlink. Do not perform a live rollback while Test jobs or leases are
    active.
12. **Browser certification blockers:** For each profile, capture Test DOM and
    prove selector uniqueness, explicit waits, post-action readback, session
    expiry handling, retry idempotency, durable checkpoints, resume inspection,
    and exact reopen verification before enabling any consequential write.

## Promotion rule

This candidate is not eligible for Production. After the missing browser layer
is implemented, the new `ezlynx.policy_setup` job type still requires three
clean Test post-job audits and the action gate requires recorded Test evidence.
Production release remains a separate, explicitly approved workstream.
