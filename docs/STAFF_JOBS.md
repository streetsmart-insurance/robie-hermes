# Staff automation jobs: meeting synthesis, staff fun, holiday alerts

Three scheduled jobs on the ROBIE job engine. Each follows the standard
recurring-job pattern: `OperationsStore.ensure_recurring_job` registers the
schedule, `robie-scheduler` claims due schedules each minute, and the bounded
engine runs them through a Worker + Verifier pair.

## Job 1 — Weekly meeting synthesis (`meeting.synthesis.weekly`)

- **Schedule:** Mondays 8:00 AM America/New_York (`0 8 * * 1`).
- **What it does:** lists Google Drive docs named `*Notes by Gemini*`
  modified in the prior 7 days, dedupes repeat saves by (title, timestamp),
  skips empty notes, classifies each note's department by the keyword map in
  `robie_job_engine/meeting_synthesis.py` (`DEPT_KEYWORDS`), synthesizes
  per-department sentiment / wins / struggles with the Gemini API, emails the
  synthesis to carlo@streetsmart.insurance **from
  robie@streetsmart.insurance**, and appends 2–3 social-post drafts (from the
  wins) to the **"StreetSmart social drafts"** Google Doc. Nothing is ever
  auto-posted to social media.
- **Department mapping:** unmatched titles (1:1s, AM syncs, meetings with
  Carlo) go to the `General / 1:1s` bucket. Carlo can correct
  `DEPT_KEYWORDS` in a follow-up PR.

## Job 2 — Monthly staff fun (`staff.fun.monthly`)

- **Schedule:** 1st of each month, 9:00 AM America/New_York (`0 9 1 * *`).
- **What it does:** looks up the month's activity in the 12-month
  `FUN_PLAN` (Oct 2026–Sep 2027) in `robie_job_engine/staff_fun.py`,
  generates a fresh, seasonally timely announcement with the Gemini API,
  posts it to the general Google Chat space via incoming webhook, creates a
  Google Form for entry/vote activities and links it (falls back to
  thread replies if Form creation fails), and emails Carlo a gift-card
  budget/fulfillment reminder (prizes stay manual).

## Job 3 — Holiday office alerts (`staff.holiday.alert`)

- **Schedule:** weekdays 9:00 AM America/New_York (`0 9 * * 1-5`).
- **Source of truth:** Google Doc `Holiday Schedule & Out-of-Office SOP`
  (`1_sE6cCkyu0SuqucWU6z-FOnKqGBQkTTe02HtoeFv784`). Every run exports the Doc
  as HTML and reads **every row** of the table under “2026–2027 Holiday
  Schedule” (Holiday / event, Date, Office status). There is no hard-coded
  holiday list. Dates in that table are smart chips; a Docs API text read
  drops them (`Monday, ` with no date), so the job uses the HTML export
  (markdown tables are also accepted). If the heading or table cannot be
  parsed, the job **fails closed** and sends nothing.
- **Window:** `ALERT_LEAD_DAYS = 7` in `robie_job_engine/staff_holiday_alert.py`
  (override with `ROBIE_HOLIDAY_ALERT_LEAD_DAYS`). An event is due when it is
  today or up to that many calendar days ahead in America/New_York. Past
  dates are skipped. While the send flags are on, the first due run sends
  and later runs dedup. While the flags are off, every run only prints the plan.
- **Copy:** deterministic templates (no Gemini). CLOSED vs close-early time,
  plan-ahead, client awareness, Gmail vacation reply, Google Calendar, and
  RingCentral, plus the Doc link. Chat copy may include emoji.
- **Row shapes:** a multi-day “and” date becomes one alert per day. A
  combined status such as Memorial Day (`Closed; close early Friday …`)
  emits both the closed day and the early-close day. `Observed Friday, Jul 3`
  is dropped when that Friday already has an explicit row (the Jul 3 early
  close), so one day does not get two emails. Black Friday on the Doc is a
  normal closed-day row and is not special-cased.
- **Dedup:** the job database table `holiday_alert_sends` records
  `(event_date, status_kind)`. A second run will not send that pair again.
  Dry-run does not write the ledger. A pending row with no receipt is held
  and not resent (fail closed against a duplicate). Clear a stuck pending
  row only after checking that the email was not already sent:

  ```sql
  DELETE FROM holiday_alert_sends
  WHERE event_date='YYYY-MM-DD' AND status_kind='closed' AND state='pending'
    AND COALESCE(email_message_id, '')='';
  ```
- **Channels, default OFF:**
  - Email to `StreetSmart@streetsmart.insurance` from
    `robie@streetsmart.insurance` (same delegated Gmail path as the other
    staff jobs).
  - Optional Google Chat via `post_chat_webhook` /
    `streetsmart-general-chat-webhook`. Incoming webhooks post as the
    webhook app. They do **not** support a true `@all` mention. This job
    sends a plain space post. `@all` would need the Chat API with a
    principal that is allowed to mention the space, which this job does not
    use.
- **Live send stays off** until Carlo sets both the master flag and a
  channel flag on `robie-scheduler` (then restart the service):

  ```bash
  ROBIE_HOLIDAY_ALERT_SEND=true
  ROBIE_HOLIDAY_ALERT_EMAIL=true
  # optional:
  ROBIE_HOLIDAY_ALERT_CHAT=true
  ```

  Payload `dry_run=true` forces a dry run even when those flags are on.
  Payload `dry_run=false` does **not** enable sending by itself. Unset flags
  mean dry-run: the worker prints `HOLIDAY_ALERT_DRY_RUN` lines and stores
  the plan. Nothing is emailed or posted.

This job type is **not** in `deploy/job_type_gate/grandfathered.json`. On
Production the new-job-type gate holds it until three clean Test audits.
Do not treat a schedule install as permission to email the agency. No
Capability Map row is added here.

## Secrets and environment (names only — values live in Secret Manager)

| Env var | Default secret | Used by |
|---|---|---|
| `ROBIE_GEMINI_API_KEY_SECRET` | `projects/streetsmart-hermes-poc/secrets/gemini-api-key/versions/latest` | Gemini synthesis + post generation |
| `ROBIE_GENERAL_CHAT_WEBHOOK_SECRET` | `projects/streetsmart-hermes-poc/secrets/streetsmart-general-chat-webhook/versions/latest` | staff-fun chat post; optional holiday alert chat |
| `ROBIE_GMAIL_DELEGATED_SA` | `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` | Gmail send as robie@ (domain-wide delegation, proven) |
| `ROBIE_GOOGLE_TOKEN_FILE` | (already in scheduler env) | Drive / Docs / Forms API |
| `ROBIE_GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model override |
| `ROBIE_HOLIDAY_ALERT_SEND` | unset (off) | Master switch for holiday live send |
| `ROBIE_HOLIDAY_ALERT_EMAIL` | unset (off) | Holiday email channel |
| `ROBIE_HOLIDAY_ALERT_CHAT` | unset (off) | Holiday Chat channel |
| `ROBIE_HOLIDAY_ALERT_LEAD_DAYS` | `7` | Holiday alert window in calendar days |

No systemd unit changes are needed: every secret name has a working default
and the Drive token env var is already set on `robie-scheduler.service`.

## Installing the schedules (on the box)

```bash
/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.staff_jobs_schedule --db /opt/streetsmart-hermes/robie-job-engine/data/jobs.db
```

This is idempotent (`ensure_recurring_job` with `reconcile=True`) and mirrors
`scripts/install_accountability_schedules.py`. Verify with:

```bash
/opt/streetsmart-hermes/venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
from robie_job_engine.operations import OperationsStore
ops = OperationsStore("/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")
for s in ops.list_recurring_jobs():
    if s["action_type"] in ("meeting.synthesis.weekly", "staff.fun.monthly", "staff.holiday.alert"):
        print(s["task_name"], s["cron_spec"], s["timezone"], "next:", s["next_run_at"])
EOF
```

## Dry-run (from a dev machine, no sends)

```bash
python3 scripts/dry_run_staff_jobs.py --job synthesis   # reads real Drive notes, Gemini synthesis, prints email body, no send
python3 scripts/dry_run_staff_jobs.py --job fun         # generates October post, prints it, no chat post
python3 scripts/dry_run_staff_jobs.py --job holiday     # HTML-exports the Holiday Schedule Doc, prints planned alerts, no send
python3 scripts/dry_run_staff_jobs.py --job holiday --fixture tests/fixtures/holiday_schedule_2026_2027.html --today 2026-10-05
```

Dry runs use `hatch_gws_cli` for Drive access and the Secret Manager
`gemini-api-key`; they never send email, post to chat, or modify the social
drafts doc. The holiday dry run never writes `holiday_alert_sends`, even if
the send flags are present in the environment. It exports HTML rather than
calling the Docs text API, because smart-chip dates are missing from that
text.
