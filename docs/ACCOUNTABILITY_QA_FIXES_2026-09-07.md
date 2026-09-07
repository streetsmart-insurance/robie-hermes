# Accountability QA corrections — September 7, 2026

Requirement: Carlo's QA handoff on PR #178. This candidate is stacked on
`codex/accountability-unified-evidence` at
`623630fddf97f7733604aa14ec1c7ccbc908d5f5` and is for Reliability / QA review.
Branch: `fix/accountability-qa-handoff`. No deployment or release certification.

## Corrections

- Daily RingCentral and Magellan collection now uses `rules.holiday_calendar`
  when selecting the prior business day. The same calendar reaches the Sheets
  `{previous_business_week}` token. With the existing `US-FEDERAL` setting,
  September 8, 2026 selects September 4 and the `8/31-9/6` weekly tab.
- The calendar implements the standard nationwide federal holidays and weekday
  observances, including a New Year's observance in the preceding year. An absent
  calendar preserves weekday-only behavior; an unknown name raises an error.
  Agency-specific or exceptional government closures are not inferred. Source:
  [OPM holiday schedule](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/).
  This change selects report dates; it does not change callback SLA clocks or
  install a holiday-aware production scheduler.
- Activity Detail resolves the latest Task ID state before applying open/overdue
  eligibility. Task modification time takes precedence over discussion creation
  time; discussion time selects the latest note within that task state. A newer
  closure, future due date, or removed due date cannot leave an older row overdue.
- Magellan pagination supplies Playwright's keyword-only `arg` parameter and
  waits for a replacement row to exist, so a transient empty table is not treated
  as a completed page change. No call details or handled-state writes were added.
- PR CI and the existing protected-main Test workflow explicitly run the new
  calendar, department, Sheets, and Magellan regression modules.

## Local evidence

- The candidate checkout's complete tree and all 396 blobs were hash-verified
  against the GitHub candidate. The original working copy was not changed.
- Focused checks: 35 passed.
- Accountability pipeline specified by CI: 110 passed.
- Baseline countercheck: the added worker, task-state and pagination checks
  produce 8 failures against the original candidate (12 other tests pass).
- Python compilation and job-type gate pass. Chat HTTP bridge: 5 passed.
  Canonical macOS path checks: 2 passed.
- The initial full suite recorded 891 passed, 5 failed and 2 skipped. Three
  failures were unavailable browser binaries; two were macOS `install` versus
  Linux `install -D` incompatibility in existing rollback tests. The same two
  rollback failures reproduce on the original candidate. The video tests require
  ffmpeg. This is not a full-suite or simulator pass.
- After installing an isolated browser, the browser fixture rerun passed all 5
  tests using synthetic pages. Chromium needed execution outside the local
  sandbox. No authenticated browser profile or live agency application was used.
- The initial simulator failed those same local browser/rollback checks; its
  service-account, secrets, browser and job-database parity remain INCONCLUSIVE.
- Test release/digest, deployed rollback target and live evidence: UNVERIFIED.
  No Test or Production deployment, scheduler change, customer dial or delivery
  was performed. Generated synthetic test state is excluded from Git/release.

## Remaining acceptance work

1. Reliability / QA must run the Linux verification gate on the final commit and
   exercise the exact immutable artifact in Test. Green CI alone is insufficient.
2. Reconcile the department report to approved active employees and live Reports
   5.0 exports. Configuration flags are not proof of field mappings. Confirm
   Task IDs, modification timestamps and treatment of contradictory same-time
   rows. The existing no-ID fallback cannot reliably join rescheduled tasks.
3. Reconcile RingCentral missed calls, queue ownership and explicit hold/wait
   fields against source evidence. Do not infer hold duration from call length.
4. Magellan needs live Test proof for empty Sad results, ordering before the
   older-date early-stop boundary, pagination limits and partial failure. The
   current collector still assumes date ordering and waits for at least one row;
   those cases are not certified by the pagination fix. Verify downstream phone
   masking: the existing `caller_phone_masked` snapshot field contains a raw
   number. Do not publish that field as masked without correcting it.
5. Confirm COI timing against the Certificates SOP's one-hour processing target.
   Keep pending-endorsement escalation separate. Date-only current-month data
   cannot prove hourly compliance or completeness of prior-month open items.
   Decide explicit historical ranges/carry-forward source before automating it.
6. Establish the dedicated accountability release path. Production target is
   `streetsmart-accountability-prod`; the repository's general Production workflow
   targets `hermes-poc-01`. Do not promote through the wrong installer. Preserve
   the prior verified digest and rehearse rollback in Test before Carlo approves
   the exact candidate digest. This PR does not install the 6:25 AM run.

## Exact Test scenarios

- September 8 at 6:25 AM Eastern: RingCentral and Magellan date September 4,
  policy tracker week `8/31-9/6`; ordinary weekday and year-boundary controls.
- Unknown holiday calendar: collection refuses rather than silently using weekdays.
- Duplicate Task ID: older open then newer closed, future due date or blank due
  date, in both input orders; reopened task counted once with latest discussion.
- Magellan: two pages for the target date, duplicate call IDs, loading gap,
  empty Sad results, unsorted rows, limit exhaustion and session expiration.
- Required input missing/stale/partial: no successful completion or team delivery.
- Read back the report totals and each department's supporting rows before signoff.
