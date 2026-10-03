# Staff automation jobs: meeting synthesis + staff fun

Two scheduled jobs on the ROBIE job engine. Both follow the standard
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

## Secrets and environment (names only — values live in Secret Manager)

| Env var | Default secret | Used by |
|---|---|---|
| `ROBIE_GEMINI_API_KEY_SECRET` | `projects/streetsmart-hermes-poc/secrets/gemini-api-key/versions/latest` | Gemini synthesis + post generation |
| `ROBIE_GENERAL_CHAT_WEBHOOK_SECRET` | `projects/streetsmart-hermes-poc/secrets/streetsmart-general-chat-webhook/versions/latest` | staff-fun chat post |
| `ROBIE_GMAIL_DELEGATED_SA` | `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` | Gmail send as robie@ (domain-wide delegation, proven) |
| `ROBIE_GOOGLE_TOKEN_FILE` | (already in scheduler env) | Drive / Docs / Forms API |
| `ROBIE_GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model override |

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
    if s["action_type"] in ("meeting.synthesis.weekly", "staff.fun.monthly"):
        print(s["task_name"], s["cron_spec"], s["timezone"], "next:", s["next_run_at"])
EOF
```

## Dry-run (from a dev machine, no sends)

```bash
python3 scripts/dry_run_staff_jobs.py --job synthesis   # reads real Drive notes, Gemini synthesis, prints email body, no send
python3 scripts/dry_run_staff_jobs.py --job fun         # generates October post, prints it, no chat post
```

Dry runs use `hatch_gws_cli` for Drive access and the Secret Manager
`gemini-api-key`; they never send email, post to chat, or modify the social
drafts doc.
