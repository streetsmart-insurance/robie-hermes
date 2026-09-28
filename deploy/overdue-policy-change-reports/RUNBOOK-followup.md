# 4359 daily policy-change follow-up runner — runbook (phase 2)

## What it does

Every morning at 07:30 ET, the runner closes the loop on the weekly
Tuesday CSR nags (phase 1):

1. **Reply tracking** — reads CSR replies in `robie@streetsmart.insurance`,
   matches each reply to the nag thread phase 1 recorded (thread IDs in
   `sent.json`), and classifies it conservatively:
   `in_progress` / `blocked` / `docs_claimed` / `no_signal`.
   Ambiguous, automated (out-of-office), or wrong-policy replies never
   move a change's status. A genuine signal suppresses further Tuesday
   nags for that change (phase 1 consults `followup.json`).
2. **Confirmation checking** — for `docs_claimed` changes, searches the
   applicant's documents via the read-only EZLynx DocumentApi. The ONLY
   machine confirmation is the change dropping off the open 4359 queue
   (the EZLynx request itself closed). Anything else is `needs_human`
   (both sides summarized, never guessed) or `discrepancy` (exact field
   mismatches, e.g. the reply names a different policy number).
3. **14-day escalation** — on Tuesdays, emails
   `carlo@streetsmart.insurance` (`Policy changes unconfirmed after 14
   days`) listing every change whose first nag is MORE than 14 days ago
   and which is not confirmed. Boundary: 13 days = no, 14 = no, 15 =
   yes. Each change escalates once (recorded, never duplicated).

The worker **never writes to EZLynx** and never deletes anything. The
only writes are the escalation email (live mode, Tuesdays only) and the
state files. It fails closed (sends nothing, persists nothing usable)
when: the 4359 report is missing/stale, the mailbox is unconfigured, or
the DocumentApi is unreachable (confirmation for that change is skipped
with an event logged).

## State

All state lives in
`/opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/`:

- `sent.json` — phase 1's store, extended with `message_id`/`thread_id`
  per nag (backward compatible with the plain-date seed entries).
- `followup.json` — per-change status, reply history, confirmation
  verdicts, escalation dates. Keyed by phase 1's notification key
  (CSR + policy number + ISO created date).
- `evidence-followup-latest.json` — the latest run's evidence.

## Modes

- `dry-run` (default, and PINNED in the systemd unit): reply ingestion
  and confirmation verdicts run for real; the escalation email is faked
  (captured in evidence as `would_send_escalation`); no state file is
  modified.
- `live`: sends the Tuesday digest and persists state.

**Do not flip `ROBIE_4359_FOLLOWUP_MODE` to `live` until Carlo reviews
the live-data dry-run evidence and explicitly approves.**

## Deploy

Same governed path as phase 1: feature branch → PR → CI green → merge →
separate release → deploy through the `/opt/streetsmart-hermes/4359-weekly`
symlink (never flip `/opt/streetsmart-hermes/current`). Then:

```
sudo cp deploy/systemd/robie-4359-policy-change-followup.{service,timer} \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robie-4359-policy-change-followup.timer
```

Manual dry-run proof (live data, nothing sent, nothing mutated):

```
sudo -u streetsmart-hermes /opt/streetsmart-hermes/venv/bin/python \
  -m robie_job_engine.run_policy_change_followup --mode dry-run \
  --evidence-out /opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/evidence-followup-latest.json
```

Entry point: `robie_job_engine/run_policy_change_followup.py`
Tests: `tests/test_4359_followup_battery.py` (43 tests)
