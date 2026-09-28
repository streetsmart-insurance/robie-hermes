# robie-cert-sweep — box-native timer RUNBOOK

Replaces the sandbox cron worker (`certificate-inbox-sweep`, every 5 min)
that SSHed through an IAP tunnel. The sweep now runs ON hermes-poc-01 as
a systemd timer: no tunnel, no per-run SSH, no bare `python`.

## What the timer runs

`robie-cert-sweep.timer` → `robie-cert-sweep.service` (oneshot) →

```
/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.cert_sweep_service
```

`cert_sweep_service` = production wiring + `cert_sweep.run_sweep()` +
`cert_notify.notify_summary()`. The service runs as
`sa_112650695780807418521` (the same OS user the sweep has always run
as) so the ledger, Gmail key, and Zapier trigger resolve unchanged.

Key env (in the unit; overrides in `/etc/streetsmart-hermes/robie-cert-sweep.env`):

- `CERT_SWEEP_DATA_DIR=/home/sa_112650695780807418521/.cert-sweep`
  (the LIVE ledger — never change this without migrating the DB)
- `CERT_APPLICANT_INDEX_PATH=/opt/streetsmart-hermes/robie-job-engine/data/cert-applicant-index.csv`
- `CERT_ZAPIER_TRIGGER=/home/sa_112650695780807418521/workspace/skills/zapier/bin/zap-trigger`
- `CERT_CALLBACK_SECRET` is fetched at runtime from Secret Manager
  (`cert-callback-secret`) by the service wrapper — never in the unit.

## Install (on hermes-poc-01, as root, after the release is deployed)

```bash
# 1. Copy the units from the deployed release
cp /opt/streetsmart-hermes/current/deploy/systemd/robie-cert-sweep.{service,timer} \
   /etc/systemd/system/
systemctl daemon-reload

# 2. Pre-flight: ledger, index, trigger, Chat identity
ls -la /home/sa_112650695780807418521/.cert-sweep/cert-sweep.db
ls -la /opt/streetsmart-hermes/robie-job-engine/data/cert-applicant-index.csv
ls -la /home/sa_112650695780807418521/workspace/skills/zapier/bin/zap-trigger
# Chat notifications need ROBIE_CHAT_SA_KEY_FILE + ROBIE_CHAT_APP_CLIENT_EMAIL
# (see chat_app_post.py); without them the notifier logs locally and skips Chat.

# 3. Dry-run the service once (does NOT enable the timer)
/opt/streetsmart-hermes/venv/bin/python - <<'EOF'
import os
os.environ["PYTHONPATH"] = "/opt/streetsmart-hermes/current"
EOF
systemctl start robie-cert-sweep.service
journalctl -u robie-cert-sweep.service --since "10 min ago" | tail -40

# 4. Enable the timer ONLY after the dry run is clean
systemctl enable --now robie-cert-sweep.timer
systemctl list-timers robie-cert-sweep.timer
```

## Retiring the sandbox cron (do this AFTER the timer is proven)

The old `certificate-inbox-sweep` interval worker must be disabled so two
schedulers never run the sweep concurrently. This is a scheduler-side
change (Muse cron), not a box change — disable it only after:

- the timer has fired at least twice cleanly (`systemctl list-timers`,
  journal shows exit 0 and sane JSON summaries), and
- one controlled production sweep proved: historical backlog parked
  (`stats.parked_historical` > 0, `stats.retried` ~ 0), current mail
  evaluated, and the notifier delivered (or logged) its outcome.

## Rollback

```bash
systemctl disable --now robie-cert-sweep.timer
# then re-enable the sandbox cron worker
```

The ledger is untouched by rollback (single SQLite file, WAL mode).

## Index refresh (governed)

The applicant index CSV is rebuilt from a fresh full-book export with
sanity gates — never by hand-editing:

```bash
/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.cert_index_refresh \
  --source /path/to/full-book-export.xlsx \
  --live-csv /opt/streetsmart-hermes/robie-job-engine/data/cert-applicant-index.csv \
  --data-dir /home/sa_112650695780807418521/.cert-sweep \
  --mark-resolved
# review the JSON report (row ratio gate, names added/removed,
# misses_now_covered), then re-run with --apply
```

## Monitoring

- `journalctl -u robie-cert-sweep.service --since "1 hour ago"` — per-run JSON.
- `/home/sa_112650695780807418521/.cert-sweep/notifications.jsonl` — every
  noteworthy run (filings, UNVERIFIED, errors).
- `sqlite3 /home/sa_112650695780807418521/.cert-sweep/cert-sweep.db \
  "select count(*) from cert_sweep_retry where attempts > 288;"` —
  parked rows (historical backlog + dead messages).
- `sqlite3 ... "select name_key, occurrences from cert_index_misses \
  where resolved_applicant_id is null;"` — current NO_MATCH names
  awaiting the refresh loop or a governed EZLynx lookup.
