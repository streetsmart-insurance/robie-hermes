# Certificates Inbox Triage — separate Phase 1 handoff

## Why and scope

Preserve the original written certificate request before matching: the original
is available for human review, and a stable source key prevents duplicate tasks.
Match the correct account/policy before assignment to avoid cross-client work.
The time savings are removing manual Gmail-to-EZLynx re-entry. Intake is accepted
only after the assigned task is independently verified in EZLynx; it is not
certificate issuance or coverage verification.

The current component reads a selected original Gmail message and attachments,
preserves it privately, requires exact matching, and prepares a task for a
configured, verified Certificates team member. It checks current active-user
status separately. Missing, conflicting, ambiguous, stale or incomplete evidence
holds the item with its original reference. Source replay and uncertain-write
reconciliation retain the shared duplicate-prevention contract.

## Certificates assignment integration

The adapter must implement
`lookup_certificates_team_member(user_id) -> ReadResult` with exactly one row:
`user_id` and `certificates_team_member: true`. This is an internal adapter
contract, not a documented EZLynx endpoint. Back it with an approved, current
Certificates team roster and report incomplete/unavailable evidence honestly.
Do not infer membership from a display name or from active-user status alone.
The configured numerical assignee and roster authority still need confirmation.
This version assigns individual users; queue assignment is not implemented.

## Document gap and later phases

Every task includes MANUAL UPLOAD REQUIRED and a source reference. A human must
upload and verify the original and then process the request. Although the supplied
Postman collection lists document endpoints, tenant availability remains
unverified. This does not block preparing the rest of the task integration.

Phase 2 adds request classification, automatic discussion notes and the active
master-certificate check under the SOP. None of those are implemented here.
Phase 3 adds autonomous production scheduling after Test evidence and release
approval. Hello and Progressive remain separate processes.

## Remaining connection and acceptance work

- Confirm the actual certificates mailbox and dedicated read-only service identity.
  Only selected-message ingestion exists; an arrivals listener/checkpoint adapter
  is still required. Do not change email labels or archive messages on a receipt.
- Connect existing server applicant/policy matching, task search, related-work
  lookup, idempotent task creation, and independent task read-back.
- Confirm Certificates roster, assigned user and due-time/business-hours policy.
  The SOP's one-hour target needs an agreed operational calendar; no calendar is
  invented in this component.
- Configure private retained-source storage and a retrieval path usable by staff.
- Register a durable Job Engine adapter and persist holds and destination evidence.

Test correct team assignment, no/ambiguous match, inactive/non-team users, missing
document access, duplicate delivery, session expiry and restart after uncertain
write. Verify the actual EZLynx task and human retrieval of the original. Current
tests are synthetic only: no live task, inbox listener, issuance or deployment
has occurred. Test release/digest and rollback target remain UNVERIFIED.

Source of truth: [Certificates SOP](https://docs.google.com/document/d/18-rHfPe4pe0pUon_I3Vr9NpVFX3DNij_GvkhgcNslOU/edit)
and Carlo's separate Certificates handoff. Do not invent unresolved discussion
titles or Automation Center details from the SOP. Production requires the
repository's separate exact-digest approval process.
