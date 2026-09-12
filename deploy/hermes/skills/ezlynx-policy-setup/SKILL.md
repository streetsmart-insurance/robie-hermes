---
name: "ezlynx-policy-setup"
description: "Prepare and verify EZLynx Policy Setup intake. Test-only Homeowners synthetic drill may save under exact constraints; every other profile remains non-writing."
job_type: "ezlynx.policy_setup"
version: "0.2.0-test"
status: "Testing"
production_ready: false
---

# EZLynx Policy Setup v0.2.0-test

Use this user-facing skill for synthetic test-account drills with sanitized identity data. Read
`references/profiles.json` before classifying the requested LOB. Every profile
is `Testing`; none is Certified or Production. Only the exact synthetic
Homeowners Test drill may perform a consequential Save.
Read `references/selector-inventory.md` before any browser-level Test and treat
every listed selector/page object as uncertified until QA supplies live proof.
EZLynx browser work is playwright_exec calls only, never execute_code, never a Python script wrapper — except a homeowners policy on applicant 220250093, which must call the `ezlynx_policy_setup` tool instead of playwright_exec. The tool runs the Job Engine path (search-first gold create, Save & Continue Edit, coverages by literal label) and returns its evidence report.

## Mandatory result states

- Return `NEEDS_SKILL` for an unsupported LOB or variant, including trucking
  under Commercial Auto — Contractors and unsupported bond types.
- Return `NEEDS_CLARIFICATION` for missing, unclear, conflicting, or guarded
  information. Never infer a value from a default or a neighboring field.
- `READY` authorizes a Save only when the Homeowners request satisfies every
  Test-only constraint below. It never authorizes bind, payment, email, a
  real-client transaction, or `COMPLETE`.

## Test-only execution contract

1. Confirm `ROBIE_ENV=TEST`, or the authorized Production test-account exception:
   `ROBIE_ENV=PRODUCTION` and `production_job_applicant()` from
   `robie_job_engine.ezlynx_write_scope` returns exactly `220250093` for the
   current RUNNING job. That helper checks the installed Production host,
   canonical ledger, and immutable original request. Never change environment
   flags to pass this check. Require sanitized identity data in either case.
2. Require account `ROBIE Test LLC`, applicant ID `220250093`, a policy number
   beginning `TEST-HO-`, an exact `$1.00` full-term premium, synthetic identity
   data, and explicit user authorization to save. Never copy a real insured,
   address, phone, email, mortgagee, or loan number into the drill.
3. Build the duplicate identity from applicant, carrier, policy number, term,
   and LOB. Search EZLynx for that exact identity before any proposed write.
4. Use stable page-object locators, unique-write enforcement, explicit state
   waits, and post-action assertions. Recorded coordinates are forbidden.
5. Persist `duplicate_check`, `pre_save_snapshot`, `post_save_readback`, and
   `reopen_verification` checkpoints around every consequential Save.
6. On resume, inspect the current EZLynx destination and checkpoints before
   repeating any action.
7. Reopen the exact policy and compare the structured expected-versus-actual
   contract. Screenshot, trace, checkpoint log, and structured verification
   result belong in the job evidence directory.
8. Never return `COMPLETE` without independent destination verification. This
   draft always records `authorizes_complete=false`.

## Homeowners Playwright rules proven in Test

- Start from `/web/account/220250093/policies`, verify the visible account
  link says `ROBIE Test LLC`, and use the exact Add Policy route. Never search
  for a nearby account or enumerate guessed Policy URLs.
- EZLynx renders hidden duplicate controls. Every write must resolve one
  visible control (for example, an exact id plus `:visible`). Never use
  `.first`, `.last`, `.nth`, or a coordinate to resolve ambiguity.
- After Add & Edit, the numeric FormEntry URL omits the applicant ID. Continue
  only when the runtime guard independently reads the exact ROBIE Test account
  link, a visible `TEST-HO-...` policy number, Homeowners LOB, and `$1.00`
  premium. Any missing or conflicting attestation is
  `EZLYNX_WRITE_SCOPE_REFUSED`.
- Use the PDF or other authorized attachment as the coverage authority while
  replacing identity fields with the approved synthetic fixture. Do not infer
  values that the source marks blank or `SEE BELOW`.
- The `Dwelling Information / Coverages` step can render only a Locations
  table for some manually created policies. If authoritative coverage or
  deductible controls are absent, stop and report the missing section. Never
  put limits into Remarks or another convenient field.
- Before Save & Close, verify the visible policy number, `$1.00` premium,
  synthetic location, and synthetic additional interest. After saving, wait
  for processing, read the server-backed Summary, and reopen the exact policy
  for the required verification checkpoint.

## Add Note pane behavior

EZLynx opens Add Note as a separate right-side website pane over the current
page. Keep it closed unless the user or approved workflow explicitly requests
a note. If it obscures the working form, close it with its X. When a note is
required, the pane or surrounding UI may be temporarily condensed; restore the
view and return to the underlying page after the note is saved and verified.

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

`consequential_writes_enabled` is `true` only for the Homeowners profile when
all exact synthetic drill constraints pass: Test environment or the active
Production test-account job exception above, applicant `220250093`,
policy prefix `TEST-HO-`, premium `$1.00`, an explicitly synthetic fixture,
and explicit Save authorization. Every other profile remains `false`. The
legacy `EzlynxPolicySetupPage.setup_policy_by_lob` multi-LOB save orchestrator
remains unapproved; use the guarded Test browser flow and fail closed whenever
the required checkpoints or destination read-back cannot be produced.
