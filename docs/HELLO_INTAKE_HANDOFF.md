# Hello Inbox — Phase 1 routing update

Carlo's separate Hello handoff supersedes the earlier shared plan: SOP ownership
routing belongs in Phase 1 for Hello. Certificates and Progressive behavior is
unchanged. This is a Test-only candidate, not a live inbox listener or deployment.

Preserve the original before matching so retries and human review retain their
evidence. Match before assigning so work reaches the correct client. The intended
benefit is removing manual Gmail-to-EZLynx re-entry; a receipt alone is not done.
Keep an explicit manual-upload obligation until document availability and upload
verification are established for the agency.

The caller supplies a human-confirmed `request_type`: `new_business`, `renewal`,
or `midterm`. `HelloIntake.perform` and `run_selected` resolve the originating
Producer for new business and applicable CSR for renewal/midterm. Service items
require a matched policy. Missing, ambiguous, stale, conflicting or inactive
ownership holds the preserved item. An optional explicit assignee is a consistency
check only and cannot override the resolved owner. Unknown request types and SOP
classification codes hold for review; automatic classification remains Phase 2.

## Integration contract still needed

The existing server adapter must implement
`lookup_hello_owner(applicant_id, policy_id, request_type) -> ReadResult`.
This is an internal contract, not a claimed EZLynx endpoint. Return one fresh,
complete, authoritative row containing the exact matched `applicant_id`,
`policy_id` (empty only for account-level new business), `role`
(`originating_producer` or `applicable_csr`) and `user_id`.
If multiple originating opportunities or policy owners are possible, hold rather
than choose. Confirm actual server field mappings; do not assume the generic
account Producer is the originating Producer. Core intake independently checks
the chosen user's active status. Routing rationale is retained in the task
description and covered by destination read-back.

Still required: the existing API connection, ownership and contact/general-match
mapping, task search/create/read-back and durable duplicate protection, Hello
mailbox identity/read permissions, staff-accessible private source storage,
due-time rules and durable Job Engine wiring. No credentials are copied from
Jake's browser. No real tasks or customer records were changed.

QA must verify all three routing cases on approved Test fixtures, missing or
ambiguous ownership, inactive owners, conflicting assignments, duplicate emails,
changed request type on replay, and restart after an uncertain write. Confirm the
actual destination task and source retrieval. Test release/digest and rollback
target remain UNVERIFIED. Production follows the separate approval process.

Source: [Hello / Mail Sorting SOP](https://docs.google.com/document/d/1ypTB9sf1TaSPA0F_hW8ODezvFQpQRln-BywiNwcGW40/edit)
and Carlo's separate Hello handoff supplied September 7, 2026. Progressive sections
are outside this update. Existing integration notes remain in INBOX_TRIAGE_PHASE1.md.
