---
name: "ezlynx-policy-setup"
description: "Prepare and verify EZLynx Policy Setup intake for the supported personal and commercial LOB profiles. Draft Test-only package; consequential writes are disabled."
job_type: "ezlynx.policy_setup"
version: "0.1.0-draft"
status: "Testing"
production_ready: false
---

# EZLynx Policy Setup v0.1.0-draft

Use this user-facing skill only in Test with sanitized fixtures. Read
`references/profiles.json` before classifying the requested LOB. Every profile
is `Testing`; none is Certified or Production.
Read `references/selector-inventory.md` before any browser-level Test and treat
every listed selector/page object as uncertified until QA supplies live proof.

## Mandatory result states

- Return `NEEDS_SKILL` for an unsupported LOB or variant, including trucking
  under Commercial Auto — Contractors and unsupported bond types.
- Return `NEEDS_CLARIFICATION` for missing, unclear, conflicting, or guarded
  information. Never infer a value from a default or a neighboring field.
- `READY` means the intake contract and duplicate identity can be tested. It
  does not authorize a Save, bind, payment, email, real-client transaction, or
  `COMPLETE`.

## Test-only execution contract

1. Confirm `ROBIE_ENV=TEST` and a sanitized approved fixture before opening
   EZLynx. Stop otherwise.
2. Build the duplicate identity from applicant, carrier, policy number, term,
   and LOB. Search EZLynx for that exact identity before any proposed write.
3. Use stable page-object locators, unique-write enforcement, explicit state
   waits, and post-action assertions. Recorded coordinates are forbidden.
4. Persist `duplicate_check`, `pre_save_snapshot`, `post_save_readback`, and
   `reopen_verification` checkpoints around every consequential Save.
5. On resume, inspect the current EZLynx destination and checkpoints before
   repeating any action.
6. Reopen the exact policy and compare the structured expected-versus-actual
   contract. Screenshot, trace, checkpoint log, and structured verification
   result belong in the job evidence directory.
7. Never return `COMPLETE` without independent destination verification. This
   draft always records `authorizes_complete=false`.

## Authoritative sources

For an actual policy, the issued policy, declarations, forms, schedules,
applications, and bind request are authoritative. The Policy Entry (APE)
spreadsheet and approved Loom recordings describe workflow behavior but do not
override an actual policy document. Redact customer information from reusable
fixtures and evidence.

## Guarded rules

Do not resolve the following without explicit approved input: Dwelling Fire
mailing versus location; Homeowners Full Value treatment; silent GL aggregate
basis; GL “if any” exposure; Garage & Dealers operation/coverage mapping; BOP
policy-level versus location-level placement; unsupported bonds; or use of the
Contractors auto profile for trucking.

## Consequential write lock

`consequential_writes_enabled` is `false` for all profiles in this version.
The legacy `EzlynxPolicySetupPage.setup_policy_by_lob` save orchestrator is not
an approved runtime path. Reliability/QA must first approve sanitized fixtures,
selector evidence, duplicate behavior, checkpoint/resume behavior, and reopen
verification for the profile under test.
