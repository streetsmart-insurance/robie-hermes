# Policy Change Request Tracker — weekly automation

Automates the Policy Change Request Tracker process doc: pulls the weekly
open change-request list from EZLynx Looker report 4659 (1-year filter, all
rows) and folds it into the "New Change Request Tracker 2" Google Sheet
weekly tab — grouped by the fixed producer teams, sorted by team then
Days Open descending, Policy Number forced to plain text, Days Open >30
flagged light red.

## Exact run command

Dry-run (no sheet writes, full pipeline through the transform + format
planners):

```bash
cd /path/to/robie-hermes
python -m robie_job_engine.reports.change_tracker.run \
  --csv /tmp/4659-export.csv \
  --total-requests 15 --total-open 12 \
  --week-tab 9/28-10/4 \
  --dry-run
```

`--csv` is a 4659 CSV export (fixture or manual download — same code path as
the live fetch, minus the browser). `--total-requests` / `--total-open` are
the two summary-tile values from the 4659 page; the run fails closed if the
downloaded row count does not equal the tile.

Live-fetch variant (drives the box EZLynx session over CDP — DOM selectors
are UNVERIFIED until the first live run):

```bash
python -m robie_job_engine.reports.change_tracker.run \
  --live --db-path /opt/streetsmart-hermes/jobs.db \
  --week-tab 9/28-10/4 \
  --dry-run
```

Live write (requires `CHANGE_TRACKER_SPREADSHEET_ID` — fails closed until
Sandeep confirms the canonical sheet, X1):

```bash
export CHANGE_TRACKER_SPREADSHEET_ID="<spreadsheet id>"
python -m robie_job_engine.reports.change_tracker.run \
  --csv /tmp/4659-export.csv \
  --total-requests 15 --total-open 12 \
  --week-tab 9/28-10/4
```

`--force` rebuilds a week tab that already holds data (otherwise the run
refuses to overwrite — idempotency). `--stats-json PATH` writes the run
stats. Exit code 2 + `FAIL_CLOSED: ...` on any validation failure.

## Layout

- `config.py` — every tunable: 4659 schema fingerprint, team roster, stale
  threshold, tab-naming rule, spreadsheet id. Sandeep's pending answers
  plug in here.
- `fetch_4659.py` — Looker 4659 export driver (CDP + expect_download,
  same conventions as `robie_job_engine.report_fetcher`): 1-year filter,
  Reload, read the two summary tiles, tile-click detail modal, "All
  results" CSV download. UNVERIFIED DOM selectors — each step fails
  loudly by design.
- `transform.py` — schema-fingerprint validation, raw→A:Q column map,
  dedupe on Policy Change Request ID, team assignment (fail closed on
  unmapped producers), sort, weekly matrix with team header rows, week-tab
  naming. No browser, no network — pure transforms.
- `sheets.py` — Sheets v4 write path (proven auth pattern: user-OAuth
  token via `ROBIE_GOOGLE_TOKEN_FILE`, default
  `/opt/streetsmart-hermes/.hermes/google_token.json`): ensure/create week
  tab, clear + rewrite + formats, row-count read-back. Formatting calls are
  UNPROVEN-UNTIL-LIVE (see ASSUMPTIONS.md).
- `run.py` — CLI wiring the pipeline together.
- `ASSUMPTIONS.md` — every assumption mapped to P1–P12 / X1–X6.
- `../../report_registry.py` — report 4659 wired in (`LOOK_ID_BY_REPORT`,
  `VERIFIED_REPORTS`); `fetch_report_rows("4659")` fails closed and points
  at the dedicated driver.

## Tests

```bash
python -m pytest tests/test_change_tracker_report.py -q
```

29 tests, fakes only, no live browser: schema fingerprint pass/fail,
column mapping, team grouping + sort, plain-text policy numbers, unmapped
producers, count cross-check, dedupe, week-tab naming, P1 open/closed
status filter + unknown-status fail-closed, P10 download retry (2-attempt
success and 3-attempt give-up), registry wiring, Sheets request shapes,
dry-run no-touch, refuse-overwrite, force rebuild.

## Status

- Unit tests: PROVEN (29/29 pass locally).
- Smoke test: PROVEN in `--dry-run` on a 7-row fixture CSV through the real
  read path (no sheet writes).
- 4659 DOM selectors: UNVERIFIED — first live fetch will likely need
  corrections.
- Sheets conditional formatting + plain-text formats: UNPROVEN-UNTIL-LIVE.
- Canonical spreadsheet id: UNSET (X1) — live writes fail closed.
- No timers/services installed. No merges. Branch only.
