# Systems Watchdog Phases 2–3 — Scheduling Guide for Dusty

**PR:** (link once created)
**What:** Extends `scripts/robie_health_check.py` with 7 new probes + a daily
digest mode, plus a standalone accountability-VM probe.

## What needs scheduling (your lane — systemd timers)

### 1. Daily digest timer (NEW)
The health check now accepts `--daily-digest`, which posts one Chat message
every morning: "all N checks green" or the failure list. This replaces
silence-means-healthy with explicit confirmation.

- **Suggested:** `robie-health-digest.timer` → `OnCalendar=*-*-* 07:00:00 America/New_York`
- **Service:** runs `/opt/streetsmart-hermes/scripts/robie_health_check.py --daily-digest`
  (status file still written; the hourly failure-only alerts are unchanged)
- **Why 07:00:** before the 9 AM accountability report, after the overnight jobs.

### 2. Accountability VM probe (NEW)
`scripts/accountability_vm_health_probe.py` runs **on** `streetsmart-accountability-prod`
(not hermes-poc-01). It checks the 5 pipeline services, 5 timers, 9 AM report
delivery timestamp, and disk. Outputs JSON to stdout + `/tmp/accountability-health/status.json`.

Two deployment options (your call):
- **A (preferred):** scp the script to the accountability VM, create
  `streetsmart-accountability-health.timer` there (`OnCalendar=*-*-* 09:30:00 America/New_York`
  and optionally `17:30`), have it POST failures to the ROBIE health Chat webhook.
- **B:** run it from hermes-poc-01 via `gcloud compute ssh streetsmart-accountability-prod`
  on a timer here, ingesting the JSON stdout.

**Note:** the probe's `REPORT_SEARCH_DIRS` may need adjusting — it searches
`/opt/streetsmart-daily-accountability/data/outputs`, `.../data/reports`, and
`.../outputs` for the newest report file. If the 9 AM report lands elsewhere,
update the list.

### 3. Existing hourly health check (NO CHANGE)
`robie-health-check.timer` stays hourly. The 7 new probes run inside it:
`phone_gmail_keys`, `login_secret_states`, `applicant_ingest_freshness`,
`eod_drive_delivery`, `task_verifier_health`, `tuesday_4359_proof`, `chat_intake`.

## New probes — what they watch

| Probe | What | Threshold |
|---|---|---|
| `phone_gmail_keys` | BOTH phone Gmail keys alive (primary + backup, separately) | Either dead = FAIL |
| `login_secret_states` | `ezlynx-username`/`ezlynx-password` have an ENABLED version (states only, never payloads) | No ENABLED = FAIL |
| `applicant_ingest_freshness` | `applicant_phone_match_export.xls` age | Warn 30h, FAIL 36h (fail-closed cliff) |
| `eod_drive_delivery` | Today's EOD xlsx exists locally | Missing on weekday = FAIL; Drive always flagged UNVERIFIED until Drive read-back exists |
| `task_verifier_health` | Verifier DB: tasks stuck PENDING/UNVERIFIED | 2h+ stuck = FAIL; journal tracebacks = FAIL |
| `tuesday_4359_proof` | `evidence-latest.json` from most recent Tuesday | Stale, failed run, or 0-sent-with-no-reason = FAIL |
| `chat_intake` | Hermes Chat listener receiving (reuses preflight logic) | Silent/wedged = FAIL |

## Known live findings (as of 2026-09-28 ~22:00 EDT)

- **`chat_intake` is FAILING in production right now:** last inbound
  2026-09-23T12:43:37Z, zero `[GoogleChat] Connected` markers in 7 days of
  `hermes-gateway.service` journal. The listener isn't initializing its
  Pub/Sub subscription — not just quiet. Needs Hermes-side investigation
  (likely your lane or the Hermes platform team).
- **`phone_gmail_keys` will report primary DEAD / backup OK** until the new
  `workspace-inbox-collector` key is minted and installed. That's expected —
  the probe is designed to show both states separately.

## Files changed

- `scripts/robie_health_check.py` — 7 new probes, `--daily-digest` flag, `send_daily_digest()`
- `scripts/accountability_vm_health_probe.py` — NEW standalone probe for the accountability VM
- `tests/test_systems_watchdog_phases_2_3.py` — NEW, 33 tests
- `docs/SYSTEMS_WATCHDOG_SCHEDULING.md` — this file
