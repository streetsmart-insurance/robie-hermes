# Inbox triage follow-up ("inbox triage follow-up") — integration plan

Status: BUILT on branch `feature/inbox-triage-followup-enrichment`, tests green,
dry-run proven on real 2026-09-22 inbox examples. NOT live: the daily triage
cron is unchanged, no Zap fires, nothing written to EZLynx.

## What was built

`robie_job_engine/inbox_triage_followup.py`:

1. **Sonant parsing** — `html_to_text()` + `parse_sonant_text()` extract Date/Time,
   Direction, Client Phone, AMS Account #, Account Name, Producer, CSR, Summary,
   Class/Type/Outcome of call, Next Step Actions. Proven on two real 2026-09-22
   "Sonant Call Analysis" emails.
   - No caller phone -> `actionable: False` (nothing to follow up).
   - Caller made no request (greeting-only / hang-up, matched by
     `NO_REQUEST_PATTERNS`) -> `actionable: False`, reason "caller made no
     request; follow-up would be noise".
2. **Task body from Sonant fields** — `sonant_task_body()` builds a plain-English
   multi-line body from the parsed fields. No LLM needed.
3. **Email summarization contract** — `EMAIL_SUMMARY_PROMPT` is the exact prompt
   the triage agent runs per missed client email; `validate_summary()`
   fail-closes on empty / >800 chars / jargon blocklist / <2 lines.
4. **Phone -> applicant matching** — `match_phone_to_applicant()` normalizes to
   10 digits and matches against the Daily Active Client Export parse
   (`applicant-directory-YYYY-MM-DD.csv`, phone_cell/phone_home/phone_work).
   No match -> human-review pile, never a guess.
5. **Payload assembly** — `assemble_task_payload()` validates:
   `task_title` present, `due_date` required ISO YYYY-MM-DD (Carlo's standing
   rule), `task_body` passes `validate_summary`, `applicant_id` numeric and not
   a known placeholder (199000001 refused).
   Payload keys: `applicant_id, task_title, task_notes, assignee, source,
   email_subject, due_date`.
6. **Approval gate** — `write_pending_proposals()` / `preview_fire()`:
   - Proposals live in
     `workspace/goals/inbox-triage-trial-carlo-s-inbox/hidden_files/pending_followups_YYYY-MM-DD.json`
     with status `pending`.
   - `preview_fire()` REFUSES unless the task exists, is pending, and
     `approved_by` names the approver of the exact contents. On approval it runs
     `zap-trigger --dry-run --applicant-verified` (validation only) and returns
     the exact live command. It never POSTs.

`tests/test_inbox_triage_followup.py`: 15 tests, all green. Fixtures are the
real 2026-09-22 Sonant emails (gmail 1a0c920af059657c, 1a0c924c267e52c7).

## Open gaps before go-live (Carlo's explicit approval needed for each)

1. **Zap update**: the agency's EZLynx follow-up-task Zap does not read
   `task_notes` today. Its Create-Task step must map the new field before any
   fire, or the summary body is dropped. (Editing the Zap = Carlo's call.)
2. **Assignee map**: `PRODUCER_USERNAMES` currently only maps
   "Carlo Ferrara" -> "Carlo1". Other producers' EZLynx login usernames must be
   resolved from EZLynx before assigning to them; until then those proposals are
   flagged for human assignee resolution.
3. **Applicant identity**: `zap-trigger` requires `--applicant-verified` —
   the applicant_id must be proven in EZLynx (profile or discussion read-back)
   before firing. The Sonant "AMS Account #" (e.g. 35246235) is UNVERIFIED as
   an EZLynx applicant ID until an API read-back confirms it.
4. **No live cron change yet**: the `inbox-triage-trial-daily` cron body is
   untouched. Proposed delta (apply only on Carlo's go-live approval):

   After step 5 (write triage-latest.json), add:
   > 5b. Follow-up enrichment (propose only, NEVER fire): for each
   > needs_reply client email, run the EMAIL_SUMMARY_PROMPT summary and
   > validate it; for each "Sonant Call Analysis" email, parse with
   > robie_job_engine/inbox_triage_followup.py and keep only actionable ones;
   > match phones against the latest applicant directory; assemble payloads
   > with due_date = next business day (or the email's real deadline);
   > write them to hidden_files/pending_followups_YYYY-MM-DD.json; include
   > each proposal (render_proposal output) in the side-chat report.
   > Do NOT fire any Zap. Firing happens only when Carlo replies with an
   > explicit "fire <task_id>", after the applicant_id is proven in EZLynx.

## Dry-run evidence (2026-09-22, nothing fired)

- Sonant Unscheduled Callback (1a0c920af059657c): parsed phone +18484487679,
  account "Kevin Hill Plumbing & Heating", producer "Taylor Cimei",
  outcome "Unscheduled Callback", actionable=True; phone had NO MATCH in the
  2026-09-22 applicant directory -> human review (honest no-guess behavior).
  Task body built and validated.
- Sonant greeting-only call (1a0c924c267e52c7): actionable=False,
  "caller made no request; follow-up would be noise".
- Lou's email (1a0bf2aaedadb988): LLM-style summary validated, payload
  assembled (due_date 2026-09-23), pending proposal written, gate refused
  without approval, and with approval validated via zap-trigger --dry-run
  (HTTP layer never touched beyond validation; LIVE FIRE NOT PERFORMED).

Pending demo file:
`workspace/goals/inbox-triage-trial-carlo-s-inbox/hidden_files/pending_followups_2026-09-22_DRYRUN-DEMO.json`
