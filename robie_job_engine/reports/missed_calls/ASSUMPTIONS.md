# ASSUMPTIONS — Missed Call Report automation

Sandeep's grill answers (M1–M15, X1–X6) from `/tmp/email-sandeep-grill.eml`:
**M1–M15 ANSWERED 2026-10-05 ~10:40 EDT** (two replies, same content).
X1–X6 still PENDING. Sandeep's sequencing: "when this report is done, we can
go to the next one" — P/E answers arrive after the missed-call report is
finalized. Each item below states what the code does TODAY; LOCKED items
encode Sandeep's verbatim answers.

Decisive feasibility findings this build follows:
- **Finding 1 (EZLynx phone search broken server-side):** the pipeline NEVER
  calls EZLynx phone search. It matches RingCentral caller numbers OFFLINE
  against the watchdog's phone index JSON built from the daily EZLynx "All
  Applicants Phone Export - Full Book v2" XLSX (report ID 4732). On ambiguity
  or a stale/missing index it FAILS CLOSED (states `ambiguous`/`unavailable`)
  and never writes "No Account" for a number it did not actually check.
- **Finding 2 (no server read path for the EZLynx Activity tab):** the
  "Was addressed?" callback determination is HUMAN-ONLY. The pipeline always
  leaves it blank. No heuristic is implemented; none is presented as a
  determination anywhere in code, docs, or output.

## M1 — Timezone
LOCKED (Sandeep: "Time zone ET"). Everything is America/New_York.
RingCentral `startTime` values are UTC ISO; each call is bucketed into the ET
calendar day, day windows are [ET midnight, next ET midnight). A call at
11:58 PM ET belongs to that ET date.

## M2 — Which results count as "missed"
LOCKED (Sandeep: "Missed, Voicemail"). Exactly {Missed, Voicemail}
(`MISSED_RESULTS` in `ringcentral_pull.py`). Abandoned / No Answer / Busy /
Rejected are EXCLUDED. There is NO minimum-duration filter: a caller who
hangs up in under 5 seconds still counts (Sandeep did not exempt short
calls).

## M3 — Monday rule vs holidays
LOCKED (Sandeep: "every date since the last business day the agency was
open"). IMPLEMENTED in `date_rules.target_dates()`: walk back from
yesterday while days are closed (weekend or agency holiday), then include
the first open business day; chronological order. Monday → Fri/Sat/Sun;
Tuesday after a Monday holiday (e.g. 2026-10-12 Columbus Day) →
Fri/Sat/Sun/Mon; any other weekday → yesterday. Agency holidays default to
US federal holidays (`DEFAULT_CLOSED_DATES`); extend per run with
`--holidays YYYY-MM-DD,...`, clear with `--ignore-default-holidays`.
`--dates` still overrides everything explicitly.

## M4 — Department attribution / agent→department roster
LOCKED (Sandeep 2026-10-05 ~10:40 EDT: "Department attribution is present on
the app sheet, administration > employees. if the department cell cant be
mapped it can be left blank. Send me an email in that case & I can help with
that."). LOCATED 2026-10-05 ~12:31 EDT (Sandeep): the "app sheet" is the
AppSheet app, Employees view:
https://www.appsheet.com/start/a1d9136f-5471-48b3-bcd7-9137cdbf6b15?platform=desktop#appName=NewApp-868850307&view=Employees
IMPLEMENTED: the Department cell carries the RingCentral-resolved employee
name when the client mapped a real person, else BLANK — never "Unknown",
never "Ext N", never a guessed department. GAP (recorded 2026-10-05): a
bounded Google Drive search found NO backing sheet for this AppSheet app
(its Drive folder "NewApp-868850307" contains only _recoveryData), so there
is no programmatic read path for the roster yet — the `--extension-map`
JSON flag remains the manual roster path. The "email Sandeep on unmapped"
step is SURFACED, not auto-sent: `pipeline.run_for_date` collects the
distinct extensions whose department cell was left blank into
`RunSummary.unmapped_departments` and prints/appends a "forward to Sandeep"
note. The operator forwards them; the pipeline never sends email.

## M5 — Same number across days / within a day
LOCKED (Sandeep 2026-10-05 ~10:40 EDT: "We want a number to be present on the
report only once. And the status for it if contacted or not."). CONFIRMED
2026-10-05 ~12:31 EDT (Sandeep: "yes"): one row per phone number PER RUN, on
the EARLIEST date's tab. IMPLEMENTED: dedupe is REPORT-WIDE within one run,
not per-day. Across all dates in one run, a number appears exactly once —
on the EARLIEST date's tab (`pipeline.run()` threads an `emitted` set
chronologically; later occurrences are dropped and counted in
`cross_date_duplicates_removed`). Within one day, multiple missed calls from
one number = one row as before. Scope note: the rule is per RUN — a number
seen on a PRIOR run's tab does NOT suppress a new row in a later run (each
run rebuilds from its own date set).

## M6 — Phone number formats
LOCKED (Sandeep: "it does not fails" — formats don't fail matching).
RingCentral API returns E.164 (`+17324628343`). Normalization strips to
digits, drops a leading US `1`, requires ≥7 digits — byte-identical to the
watchdog's `build_phone_index.py` rule. Display `(732) 462-8343` is cosmetic
(sheet column B only). EZLynx global-search formatting is IRRELEVANT: the
pipeline never uses EZLynx search (finding 1).

## M7 — Multi-account matches
LOCKED (Sandeep: "for multi accounts, we select the account that has ongoing
activity. For a vendor or carrier not a client we keep that as blank").
PARTIALLY IMPLEMENTABLE: "ongoing activity" requires reading the Activity
tab, which has NO server read path (finding 2) — so the pipeline still
fails closed: `ambiguous` → Profile cell gets plain text `AMBIGUOUS - N
accounts share this number (human review)`, no hyperlink, "Was addressed?"
blank. THE HUMAN applies Sandeep's rule (pick the account with ongoing
activity; blank the profile for vendor/carrier non-clients). If Sandeep's
stack ever gains a server-side Activity read, this becomes automatable.

## M8 — Callback window
LOCKED (Sandeep's rule, HUMAN-APPLIED per finding 2): "if the report is ran
on 2nd for the 1st day. At the time of report being done, if the activity
log says if the contact has been made/ issues has been addressed / if the
activity shows the caller intends were for COI and the COI request has been
completed, we can mark the call as addressed". No window logic exists in
this codebase — the human applies Sandeep's rule when filling
"Was addressed?".

## M9 — "Offered a callback" notes
LOCKED (Sandeep: "rule is simple, if there was a missed call/voicemail was
it addressed."). HUMAN-APPLIED (finding 2). No code.

## M10 — Voicemails with no callback
LOCKED (Sandeep: "if voicemail received, no callback made, it is NO.").
HUMAN-APPLIED (finding 2): the pipeline records the call's result
(Missed/Voicemail) in the run summary/audit and leaves the SHEET row's
"Was addressed?" blank either way; the human marks NO for unreturned
voicemails.

## M11 — No-Account numbers
LOCKED (Sandeep: "Keep it blank, team calls back."). Rows are written as
`No Account` (exact casing, plain text) with "Was addressed?" blank, per the
doc. The team works the callbacks; the pipeline creates no lead tasks and
sends no alerts (see X4).

## M12 — "Was addressed?" = No consumers
LOCKED (Sandeep: "Was addressed - this field is for the team to check if the
call back was made. And if there are no call back made, team will call back
as per the status."). The pipeline never writes Yes/No, never creates
callback tasks, wires no notifications. A "No → auto-create callback task"
step would be a separate, explicitly authorized build — not silently added.

## M13 — Human-filled vs automation-filled rows
LOCKED (Sandeep: "Updated by. No action required here"). Row identity =
normalized phone digits in column B. The writer is APPEND-ONLY: it reads the
tab's existing column-B phones and only appends numbers not already present.
It NEVER modifies or deletes an existing row. If a human edits a row the
automation added, that row is left exactly as the human left it.

## M14 — Tab creation races
LOCKED (Sandeep: "Human wins. Send me an email in that case."). If the dated
tab exists → append-only new phones (safe even if a human created it or
added rows; human content is never touched = human wins by design). If
missing → duplicate the most recent existing M/D tab, rename, clear data
rows (keep header), then write. If no dated tab exists to duplicate → FAIL
CLOSED. The "email Sandeep on conflict" step is UNWIRED (no notification
path; see X4).

## M15 — RingCentral without the browser
LOCKED (Sandeep: "No" — no existing API/export). Our path stands on its own:
`robie_job_engine/ringcentral_client.py` (JWT bearer, GET
/restapi/v1.0/account/~/call-log?view=Detailed) reused as-is; the hourly job
on the box uses it today. No new client built.

## X1 — Canonical sheet URLs
LOCKED 2026-10-05 ~12:31 EDT (Sandeep): the canonical workbook is
"Missed Calls Report 2026" at
https://docs.google.com/spreadsheets/d/1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y/edit
(id `1POQ9oAop1AOa6gWsaNVQX3I540WvvX0gmw9XNPK1L5Y`, recorded as
`CANONICAL_SPREADSHEET_ID` in `sheet_io.py` and the `--spreadsheet-id`
default). STILL NO LIVE WRITES: `--dry-run` remains the default and the
only tested mode; the write path has never run against the real workbook
(first live run must be eyeballed). No sheet writes have been performed.

## X2 — Timezones
LOCKED via M1: America/New_York for everything.

## X3 — Failure semantics
ASSUMED fail-closed: any precondition failure (RC auth/fetch error, phone
index missing/stale) aborts BEFORE any sheet write for that date and exits
2. The per-date append is a single API call; if it fails, the error is
reported and nothing is retried implicitly. Partial results are reported,
never silently shipped. NO notification path is wired (see X4) — failures
surface as the non-zero exit + stderr/stdout summary for now. (Sandeep was
not asked X3 yet; his M-sequence answers don't change this.)

## X4 — Consumers and alerting
PENDING. No alerts, emails, or chat messages are wired. The run summary
(stdout/JSON) is the only output until Sandeep names who consumes the
report and what action it drives.

## X5 — Session ownership
RingCentral: JWT server-side via GCP Secret Manager
(`ringcentral-accountability-client-id/-secret/-jwt/-server-url`) — PROVEN
by the hourly box job; this pipeline reads them the same way and never
prints values. Sheets: `ROBIE_GOOGLE_TOKEN_FILE` authorized-user creds or
ADC (pattern proven by publish_4359_liveness.py) — the WRITE path itself is
UNVERIFIED (X1). Nobody needs to keep a browser session alive; that was the
point of the API path.

## X6 — Timing
PENDING. Suggested: daily ~8:00 AM ET; Monday run covers Fri/Sat/Sun; no
weekend runs. NO timers installed or enabled (repo freeze + standing rule).

## Index freshness (no Sandeep question covers this; derived from finding 1)
The phone index on the box is currently built 2026-09-25 (~10 days old).
ASSUMED: an index older than `--index-max-age-days` (default 7) is STALE and
every lookup returns `unavailable` — a stale book cannot prove "No Account",
and applicants added after the export would be mislabeled. Fail-closed exit
2. Override the threshold explicitly per run if a fresher book is confirmed.
