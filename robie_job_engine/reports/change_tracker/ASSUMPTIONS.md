# Policy Change Request Tracker — Assumptions Register

Every assumption below is keyed to Sandeep's question numbers (P1–P12,
X1–X6). **P1–P12 are LOCKED per Sandeep's answers on 2026-10-05** (each
entry quotes his verbatim answer); X1–X6 remain open. Each entry records
the locked answer, where it is encoded (config-only plug-in, no code
changes needed to change it), and what breaks if the answer is wrong.

Status legend: LOCKED (answered by the process owner 2026-10-05; do not
change without re-confirming) · ASSUMED (config default, replaceable) ·
PROVEN (ran) · UNPROVEN-UNTIL-LIVE (implemented per API reference, not yet
run against a live tab) · DERIVED (inferred from the spec doc; needs owner
confirmation).

---

## Finding 1 — Looker 4659 wiring + schema fingerprint (PROVEN)

- Added `"4659": "4659"` to `LOOK_ID_BY_REPORT` and a `ReportSpec` to
  `VERIFIED_REPORTS` in `robie_job_engine/report_registry.py`
  (identity = `policy_change_request_id`, scope marker = look title
  "Policy Change Request Summary").
- The pinned schema fingerprint is `EXPECTED_4659_COLUMNS` in
  `robie_job_engine/reports/change_tracker/config.py`; it is re-validated
  (names AND order) on **every** export in `transform.assert_schema_fingerprint`
  — a silent EZLynx rename/reorder/add/drop raises `SchemaMismatchError`
  and ships nothing (P11).
- `fetch_report_rows(report_id="4659")` deliberately FAILS CLOSED and points
  to the dedicated driver: the generic Export-button path cannot run the
  tile-click modal flow and cannot provide the two summary-tile values the
  P10 count cross-check requires. The dedicated driver is
  `fetch_4659.fetch_4659_export` (CDP + `expect_download`, same conventions
  as `report_fetcher`).

## Finding 2 — UNPROVEN-UNTIL-LIVE (Sheets formatting)

- `sheets.build_format_requests` implements three formatting steps exactly
  per the Sheets API v4 reference: (a) Plain-Text number format on column
  J (Policy Number) only — P9 LOCKED (Sandeep 2026-10-05: keep the Plain
  Text formatting step on Policy Number every run; NO other columns need
  it); (b) bold + light-blue `#cfe2f3` fill on team header rows; (c)
  `addConditionalFormatRule` with custom formula `=$C2>30` over
  `A2:Q<last>` with fill `#f4cccc` (palette "light red 3").
- They are exercised in `--dry-run` (request SHAPES verified in unit tests)
  but have NEVER been applied to a live tab. First live run must eyeball the
  tab before anything is considered working.
- The stale formula is a single fixed `=$C2>30` rule. If Sandeep sets
  per-team thresholds (P5), that needs one rule per team range — not yet
  implemented; `STALE_DAYS_OPEN_BY_TEAM` only affects the stats count today.

## Finding 3 — Locked items encoded as CONFIG (all LOCKED 2026-10-05)

P1, P2, P4, P5, P6, P7, P12 are all in `config.py` as replaceable settings,
never hardcoded in pipeline code. See the per-question entries below.

---

## P1 — "Open" definition (LOCKED 2026-10-05)

- Sandeep's verbatim answer: `"Task Created", "ongoing", "Error" = open;
  "Done" = closed`.
- Encoded: `OPEN_REQUEST_STATUSES = ("Task Created", "ongoing", "Error")`,
  `CLOSED_REQUEST_STATUS = "Done"` (config.py). The pipeline keeps rows
  whose Request Status is in the open set, filters out "Done" rows and
  counts them in `stats.dropped_not_open` — and they still count toward the
  P10 tile check BEFORE filtering, since the tile counts all rows.
- ANY OTHER Request Status value raises fail-closed, listing the unknown
  status — never guessed.
- NOTE: whether "Done" rows actually appear in the 4659 export is
  UNCONFIRMED — the code handles them either way (a zero-Done export just
  yields `dropped_not_open=0`).
- If wrong: closed requests would be silently included in the weekly tab,
  or an unrecognized status would fail every run until Sandeep names it.

## P2 — 1-year filter drop-off (LOCKED 2026-10-05)

- Sandeep's verbatim answer: the 1-year filter stays; it has never dropped
  anything; no flag needed.
- Encoded: `CREATED_DATE_FILTER_TEXT = "is in the last 1 year"`; the live
  driver verifies the filter chip reads back as the 1-year filter after
  Reload and fails closed otherwise (a 30-day-default slice would silently
  under-report).
- No flagging path is built — per Sandeep's answer it has never dropped a
  row. If that changes, this becomes a new question for him.

## P3 — One row per change request (LOCKED 2026-10-05)

- Sandeep's verbatim answer: one row per change request confirmed.
- Encoded: `dedupe_rows` dedupes on `Policy Change Request ID`. Byte-identical
  duplicates are dropped and counted (`stats.duplicates_dropped`); a duplicate
  ID with DIFFERENT content raises `DuplicateRequestError` — fail closed,
  never guess which row is authoritative.
- Covered by `test_identical_duplicate_request_id_deduped` and
  `test_conflicting_duplicate_request_id_fails` (verified 2026-10-05).
- If a legitimate multi-policy request appears twice by design, Sandeep's
  answer changes the dedupe rule.

## P4 — Days Open rule (LOCKED 2026-10-05)

- Sandeep's verbatim answer: Days Open comes from report 4659 as-is; never
  compute it.
- Encoded: `DAYS_OPEN_RULE = "calendar"`; `transform` reads the "Days Open"
  CSV column as-is — no recompute-from-Created-Date path exists.
- Blank or non-numeric Days Open raises — never guessed.

## P5 — Stale threshold (LOCKED 2026-10-05)

- Sandeep's verbatim answer: stale threshold >30 days confirmed for all
  teams/LOBs.
- Encoded: `STALE_DAYS_OPEN = 30`, `STALE_FORMULA = "=$C2>30"`, fill
  `#f4cccc`. `STALE_DAYS_OPEN_BY_TEAM = {}` remains in config.py as an
  (empty) override extension point, but the LOCKED rule is the single
  >30-day threshold for every team — do not add overrides without
  re-confirming with Sandeep.
- If commercial/trucking carrier-driven changes routinely run longer,
  this threshold (not the override map) is what changes.

## P6 — Team roster (LOCKED 2026-10-05, fail-closed)

- Sandeep's verbatim answer: team roster source = the AppSheet employees
  view: https://www.appsheet.com/start/a1d9136f-5471-48b3-bcd7-9137cdbf6b15?platform=desktop#appName=NewApp-868850307&view=Employees
- GAP RECORDED (2026-10-05): a bounded Google Drive search did NOT find a
  backing sheet for this AppSheet app — the app's Drive folder
  `NewApp-868850307` contains only `_recoveryData`. There is currently no
  read path from the automation into the AppSheet roster, so the roster
  stays HARDCODED in `TEAM_ROSTER` for now. Procedure until the read path
  exists: roster edit in config.py + rerun.
- Encoded roster (unchanged): Personal (Jazmin Molina, Daniela Aguilar);
  Commercial (Taylor Cimei, Zeus Quezada, Sandy Santana, Carlo Ferrara,
  Andrea Illanes); Trucking (Angie Valladarez, Ricardo Aguilar,
  Jake Ferrara, Mike Sosa, Maria Bara).
- An unmapped producer FAILS THE WHOLE BUILD — `UnmappedProducerError`
  collects and lists EVERY unmapped name in the run output (verified
  2026-10-05: `test_all_unmapped_producers_listed`) — per the spec's "stop
  and ask the user, don't guess". There is NO automatic "team 9" bucket.
- Open follow-up (not a blocker): build a read path into the AppSheet
  employees view so roster changes don't need a config edit.

## P7 — Assigned Producer = owner (LOCKED 2026-10-05)

- Sandeep's verbatim answer: owner = Assigned Producer from report 4659.
- Encoded: the team grouping and header rows are built from "Assigned
  Producer".
- The spec never routes rows to AM/CSR/Robie; any owner-override mapping
  would be a future question, not an assumption.

## P8 — Quiet window / concurrent edits (LOCKED 2026-10-05)

- Sandeep's verbatim answer: nobody edits the sheet mid-build — a full
  rewrite of the weekly tab is safe.
- Encoded: the automation owns the week tab during the run. It never does
  the manual process's half-built state (raw paste at column S, then
  column-by-column moves): the full matrix is built in memory, then the tab
  is cleared + rewritten + formatted as one sequence.
- A tab that already holds data rows refuses a second write without
  `--force` (idempotency). There is no merge with human edits — a human
  edit mid-run is overwritten on the next `--force` rebuild.

## P9 — Plain-Text formatting (LOCKED 2026-10-05)

- Sandeep's verbatim answer: keep the Plain Text formatting step on Policy
  Number every run (idempotent); NO other columns need it.
- Encoded: `build_format_requests` applies `{"type": "TEXT"}` to J2:J
  (Policy Number) ONLY. The former N2:N (Policy Change Request ID) Plain
  Text request was REMOVED 2026-10-05. UNPROVEN-UNTIL-LIVE (Finding 2).

## P10 — Count cross-check (LOCKED 2026-10-05)

- Sandeep's verbatim answer: on a count mismatch vs the "Total Open Change
  Requests" tile, RETRY the download (up to 3 attempts total), then fail
  closed (exit 2) if still mismatched.
- Encoded: `run._fetch_and_build_live` wraps the live fetch + dedupe +
  tile count check. On `CountMismatchError` it re-downloads, up to
  `MAX_DOWNLOAD_ATTEMPTS = 3`. If all 3 attempts mismatch, the
  `CountMismatchError` propagates and the CLI fails closed with exit 2
  (`FAIL_CLOSED: ...`). Only `CountMismatchError` is retryable — schema,
  unknown-status, and unmapped-producer failures raise immediately.
  The attempt count is logged (`download attempt n/3`, "count cross-check
  passed on attempt n") and recorded in the stats JSON (`download_attempts`).
- The --csv mode uses a fixed file, so it fails closed on the FIRST
  mismatch (no retry — re-reading the same file would change nothing).
- Covered by `test_retry_on_count_mismatch_succeeds_on_second_attempt`
  and `test_retry_gives_up_after_three_attempts` (verified 2026-10-05).
- The check runs BEFORE any sheet write, and the message names the tile
  value, the downloaded row count, and duplicates dropped. Nothing partial
  is shipped (see X3).

## P11 — Report 4659 column changes (LOCKED 2026-10-05)

- Sandeep's verbatim answer: no changelog/contact for report 4659 column
  changes; keep the schema fingerprint check.
- The schema fingerprint (Finding 1) IS the change detector: exact
  name+order match, fail closed with a diff (missing vs unexpected columns)
  on any change. There is no EZLynx changelog or contact — detection is the
  mechanism, and the fingerprint string is logged on every run so drift is
  visible.

## P12 — Weekly tab naming (LOCKED 2026-10-05)

- Sandeep's verbatim answer: weekly tab naming = Monday–Sunday of the week
  just closed, REGARDLESS of run day or holidays.
- Encoded: `TAB_WEEK_RULE = "week-ending-sunday"`,
  `transform.week_tab_name`. week_ending = the most recent Sunday on or
  before the run date; the tab spans week_ending − 6d through week_ending,
  formatted like the spec's example "7/20-7/26" (e.g. "9/28-10/4").
- BEHAVIOR CHANGE 2026-10-05: a Sunday run now labels the week ending TODAY
  (the week just closed). Mon 10/05 → "9/28-10/4"; Wed 10/07 → "9/28-10/4";
  Sun 10/04 → "9/28-10/4" (previously "9/21-9/27"); Sat 10/03 →
  "9/21-9/27". `--week-tab` still overrides for one-offs.
- Covered by `test_week_tab_name_monday`, `test_week_tab_name_wednesday`,
  `test_week_tab_name_sunday`, `test_week_tab_name_saturday`,
  `test_week_tab_name_next_sunday` (verified 2026-10-05).

## X1 — Canonical spreadsheet (BLOCKING, PENDING)

- The automation FAILS CLOSED (`MissingSpreadsheetError`) until the
  canonical "New Change Request Tracker 2" spreadsheet id is provided —
  it will not guess among similarly-named copies.
- Provide via `CHANGE_TRACKER_SPREADSHEET_ID` env var or
  `config.py::CHANGE_TRACKER_SPREADSHEET_ID`. No sheet writes of any kind
  happen until this is set (dry-run is the only usable mode).

## X2 — Timezone (ASSUMED)

- Everything is `America/New_York` (`TIMEZONE` in config.py): the
  week-tab date math and the Days Open reference date. EZLynx timestamps
  and RingCentral are out of scope for this workflow.

## X3 — Failure semantics (ASSUMED)

- Fail closed, retry the whole thing, ship nothing partial:
  `FAIL_CLOSED_ON_PARTIAL = True`. The tab write is clear + full rewrite +
  formats followed by a row-count read-back; a read-back mismatch raises.
- Who gets notified and how is PENDING — the CLI prints `FAIL_CLOSED: ...`
  and exits 2. Alert routing (chat/email) is not built.

## X4 — Consumers / actions (PENDING)

- Unknown. This decides what the automation should alert on (e.g. a team
  with N stale requests). No alerting is built until Sandeep answers.

## X5 — Session ownership (ASSUMED)

- The live 4659 fetch drives the box's persistent EZLynx Chrome over CDP
  (`PlaywrightEzlynxSession`), following `report_fetcher` conventions —
  i.e. it reuses the existing session, never enters credentials.
- Who keeps that session alive long-term, and whether a persistent
  server-side EZLynx session is proven reliable, is PENDING (ops question).
- NOTE: the 4659 DOM selectors (filter chip, Reload, tiles, detail modal,
  "All results") are UNVERIFIED against the live UI — the first live fetch
  is expected to need selector corrections. Each step fails loudly by
  design.

## X6 — Run day/time (ASSUMED)

- Assumed: weekly Monday 06:30 America/New_York (`ASSUMED_RUN_SCHEDULE`).
- NO timer/service is installed or enabled by this change (merge/timer
  freeze in effect). Scheduling is a separate, later step after Sandeep's
  answer and Carlo's go.

---

## Spec-derived choices that still need Sandeep's confirmation (DERIVED)

1. **Columns P/Q** ("Total Policy Change Requests" / "Total Open Change
   Requests") are filled on EVERY data row with the two summary-tile
   values. The spec lists P/Q as destination columns but never says where
   their values come from; the tiles are the only source. If the team
   fills P/Q differently (or leaves them blank), this changes.
2. **Column A "Action"** is left blank by the automation (no raw source
   exists); it is the team's working column.
3. **Team header rows** use first names only ("Personal - Jazmin,
   Daniela"), bold + `#cfe2f3` light-blue fill across A:Q, matching the
   spec's example. The exact blue is ASSUMED — the reference rule is
   "match the prior week's tab", which the automation cannot see; Sandeep
   should confirm the hex or point at the reference tab.
4. **"light red 3"** is encoded as `#f4cccc` (Sheets palette value).
5. **Days Open sort is numeric descending** within each team; ties keep
   export order (stable sort).
6. **No raw-data staging in the sheet.** The manual process pastes raw at
   column S and maps column-by-column; the automation builds the final
   matrix in memory and writes it once. The raw CSV is kept only in the
   run's staging dir (`/tmp/ezlynx_reports`) for the cross-check.
