---
name: robie-ape-test
description: Train and evaluate Robie on Application/Policy Entry (APE) workflows in EZLynx using the approved APE spreadsheet, Loom demonstrations, sanitized policy fixtures, and Playwright. Use for guided APE practice, source-to-field mapping, selector capture, readiness scoring, and Test-only policy-entry dry runs.
---

# Robie APE Test

Read `references/training-pack.json` before every session. Read
`references/source-reconciliation.md` when selecting a line of business or when
the sheet, video, policy documents, and approved rules disagree.

## Fixed boundary

- Require `TEST`, the visible account identity `ROBIE Test LLC`, and
  applicant/account `220250093`. Stop `BLOCKED-CORRECTLY` if any value cannot
  be proven.
- This draft is observation and dry run only. Do not click Save, Save and
  Continue, Complete, Submit, Bind, Issue, Send, or any control that commits a
  change.
- Do not use a real client, live recipient, real payment, financing, binding,
  policy issuance, or employee-sensitive data.
- Never capture passwords, cookies, tokens, MFA values, session data, or client
  data in a recording, trace, screenshot, selector file, or evidence bundle.
- Use Playwright. Coordinate-only clicking, `.first`, `.last`, `.nth`, guessed
  selectors, and sleeps used as readiness are blocked.

## Authority order

For policy facts, the actual sanitized policy fixture and its declarations,
forms, schedules, applications, and bind request are authoritative. Current
approved SOPs outrank the APE sheet and Loom videos. The sheet and videos teach
workflow behavior; they never authorize an unsupported default.

## Guided training procedure

1. Select one workflow in the pack. Honor `NEEDS-SKILL` and
   `BLOCKED-CORRECTLY` without inventing a workaround.
2. Open only the approved Test account. Confirm the URL contains account
   `220250093`, then confirm the visible account identity is `ROBIE Test LLC`
   before continuing.
3. Compare the sanitized fixture with the exact sheet row and linked video.
   Record unclear or conflicting instructions as blockers.
4. Observe one screen at a time. Capture semantic locator candidates, required
   fields, navigation state, expected readback, and stop conditions.
5. Dry-run data mapping without committing a write. Every field must map to an
   authoritative fixture value; never copy a UI default merely because it is
   selected.
6. Record four proposed checkpoints for any future write path:
   `duplicate_check`, `pre_save_snapshot`, `post_save_readback`, and
   `reopen_verification`.
7. Score all five evaluation lanes. Keep the skill `DRAFT` until the browser,
   assignment, and verification lanes have real Test evidence.

## Scope

The pack covers the 12 profiles already recognized by Robie's draft
`ezlynx-policy-setup` contract. Other APE rows return `NEEDS-SKILL`. The
spreadsheet's Trucking tab is explicitly outside this skill.

This skill never promotes itself, changes the runtime action gate, deploys to a
VM, or claims Production readiness.
