# Missed Call Report automation

Automates Phase 1 + the automatable parts of Phase 2 of the Missed Call
Report process doc (`~/workspace/user/files/Missed_Call_Report.docx`):

1. **Pull** — RingCentral call-log API (inbound, Missed/Voicemail), per target
   date in America/New_York. Monday rule: Fri/Sat/Sun get separate runs and
   separate dated tabs.
2. **Dedupe** — one row per unique phone number per day (earliest call kept).
3. **Match** — offline against the EZLynx full-book phone index JSON
   (watchdog-built). Fail-closed on ambiguity / stale / missing index.
4. **Write** — append-only into the "Missed Calls Report 2026" workbook's
   M/D tabs. Existing rows are never modified; "Was addressed?" and
   "Updated by" are always left blank (human-only).

## Exact run command

Dry run (default, safe — writes nothing):

```bash
cd /opt/streetsmart-hermes/robie-job-engine   # or your repo checkout
PYTHONPATH=. python -m robie_job_engine.reports.missed_calls.cli
```

Useful flags:

```bash
# Explicit dates (holiday weeks, backfills) — overrides the Monday rule
PYTHONPATH=. python -m robie_job_engine.reports.missed_calls.cli \
  --dates 2026-10-02,2026-10-03,2026-10-04

# JSON summary output
PYTHONPATH=. python -m robie_job_engine.reports.missed_calls.cli --json

# LIVE sheet write (requires confirmed canonical workbook, X1 pending)
PYTHONPATH=. python -m robie_job_engine.reports.missed_calls.cli \
  --no-dry-run --spreadsheet-id <workbook-id>
```

Environment:
- `RINGCENTRAL_CLIENT_ID` / `RINGCENTRAL_CLIENT_SECRET` / `RINGCENTRAL_JWT` /
  `RINGCENTRAL_SERVER_URL` — or run on the box job identity (GCP Secret
  Manager keys `ringcentral-accountability-*`). Values are never printed.
- `ROBIE_GOOGLE_TOKEN_FILE` — authorized-user creds for Sheets (else ADC).
- `MISSED_CALL_PHONE_INDEX` — override the phone index JSON path
  (default `/opt/streetsmart-phone-watchdog/data/phone_index_fullbook.json`).

Exit codes: `0` ok · `2` fail-closed (aborted before/while writing) ·
`1` unexpected error.

## Tests

```bash
python -m pytest tests/test_missed_call_report.py -q
```

## Status / caveats

- "Was addressed?" callback judgment: HUMAN-ONLY by design (no Activity-tab
  read path server-side). The pipeline never fills it.
- Sheet writes: implemented but UNVERIFIED against the real workbook
  (canonical URLs pending, X1). `--dry-run` is the only tested mode.
- Assumptions: see `ASSUMPTIONS.md` (every Sandeep question M1–M15, X1–X6
  mapped).
