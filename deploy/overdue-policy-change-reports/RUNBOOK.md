# 4359 weekly overdue policy-change runner — runbook

## What it does

Every Monday at 08:00 ET, the runner pulls the live EZLynx **4359 Policy
Change Request Confirmation Queue** (from the daily `ROBIE daily CSV - 4359
Policy Change` email in `robie@streetsmart.insurance`), qualifies open
requests older than 14 days, gates each policy through the PolicyApi
liveness check, enriches with DiscussionApi context, and emails each CSR:

- **To:** the CSR named on the queue row
- **CC:** the CSR's department manager, `carlo@streetsmart.insurance`,
  Jake Ferrara (`jake@streetsmart.insurance`), Gabriela Chutin
  (`gabrielac@streetsmart.insurance`), Sandy Santana
  (`sandy@streetsmart.insurance`), and the assigned producer

Each email asks for a status update (what's done / what's blocked / what's
next) and carries the abbreviated policy-change SOP closure gate. The
change request's **Open** status is authoritative — a Complete EZLynx task
never suppresses the nag (that was the Arellano 2026-09-27 lesson).

The worker **never writes to EZLynx** and never deletes anything. The only
write is the outbound email. It fails closed (sends nothing) when: the 4359
report is missing/stale, the roster is unavailable, a CSR can't be resolved
to an active employee, or the DiscussionApi is down.

Dedup: a CSR + policy number + created date is re-nagged only when still
open after **RENAG_DAYS = 7**.

Entry point: `robie_job_engine/run_overdue_policy_change_reports.py`
(`python -m robie_job_engine.run_overdue_policy_change_reports`).

## Schedule

- Timer: `robie-4359-policy-change.timer` → `OnCalendar=Tue *-*-* 08:00:00 America/New_York` (every Tuesday 08:00 ET, per Carlo 2026-09-27)
- Service: `robie-4359-policy-change.service` (oneshot)
- Check: `systemctl list-timers robie-4359-policy-change.timer`
- Logs: `journalctl -u robie-4359-policy-change.service --since "7 days ago"`

## Secrets / env required on the box

| Variable | Purpose | Where it lives |
|---|---|---|
| `ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT` | Read the daily 4359 CSV from the report mailbox; send the nags (live mode) | `/etc/streetsmart-hermes/robie-accountability.env` |
| `EZLYNX_POLICY_API_*` (token endpoint, client id/secret, username, integration group id, scope) | PolicyApi liveness gate | `/etc/streetsmart-hermes/robie-ezlynx.env` |
| `EZLYNX_DISCUSSION_*` (same shape) | DiscussionApi context lookup | `/etc/streetsmart-hermes/robie-ezlynx.env` |
| `ROBIE_4359_MODE` | `dry-run` (default, sends nothing) or `live` | `Environment=` in the service file; override in `/etc/streetsmart-hermes/robie-4359.env` |
| `ROBIE_4359_MANIFEST` | Path to the approved roster manifest | `/etc/streetsmart-hermes/robie-4359-manifest.json` |
| `ROBIE_4359_SENT_STORE` | Dedup store | `/opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/sent.json` |

Additionally, the box service account
`hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` must have
**Viewer** access to the roster sheet `db 1 - StreetSmart Production Copy`
(the manifest reads only the allowlisted non-sensitive columns: Name,
Position, Email, Department, Employment Status, Department Head — never
SSN/DOB/salary/commission). **If the sheet is not shared with the box SA,
the run fails closed with "approved active-employee roster is unavailable"**
— share the sheet, then re-run. (Carlo action.)

## Install / deploy (hermes-poc-01)

The worker is deployed as its own release directory; the main production
pointer (`/opt/streetsmart-hermes/current`) is **never flipped** for this
worker. The service runs from the stable symlink `/opt/streetsmart-hermes/4359-weekly`.

1. Build the release from the merged PR commit (governed path):
   `scripts/build-release.sh` in a clean worktree, then `scripts/verify-release.sh`.
2. Copy the tarball to the box, extract to
   `/opt/streetsmart-hermes/releases/<short-sha>/robie-hermes-<short-sha>/`.
3. Re-pin the symlink: `ln -sfn /opt/streetsmart-hermes/releases/<short-sha>/robie-hermes-<short-sha> /opt/streetsmart-hermes/4359-weekly`
4. Install `/etc/streetsmart-hermes/robie-4359-manifest.json` from
   `deploy/overdue-policy-change-reports/roster-manifest.json` (root-owned, 0644).
5. Install the systemd units from `deploy/systemd/robie-4359-policy-change.{service,timer}`,
   then `systemctl daemon-reload && systemctl enable --now robie-4359-policy-change.timer`.
6. Seed the sent store with the manually-sent 2026-09-27 emails (Arellano /
   ISCA / Guarini) so the first weekly run does not re-nag them:
   `/opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.run_overdue_policy_change_reports --seed-file deploy/overdue-policy-change-reports/sent-seed-2026-09-27.json`
   (run with `PYTHONPATH=/opt/streetsmart-hermes/4359-weekly`).
7. Dry run (real queue, real EZLynx lookups, fake mailer):
   `ROBIE_4359_MODE=dry-run ... --evidence-out <path>`; inspect the evidence
   JSON for recipients, CCs, and bodies.
8. Only after the dry run is reviewed **and** Carlo/parent confirms: set
   `ROBIE_4359_MODE=live` in `/etc/streetsmart-hermes/robie-4359.env`.

## Checking a run

- `systemctl list-timers robie-4359-policy-change.timer` — next run.
- Evidence of the latest run:
  `/opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/evidence-latest.json`
  (contains the summary counts, per-CSR recipients/CCs, and — in dry-run —
  the full message bodies).
- Sent store:
  `/opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/sent.json`
  (sha256 keys → ISO dates; re-nag after 7 days).
- Delivery verification: each receipt carries the Gmail `message_id`; the
  `OverduePolicyChangeReportVerifier` reads them back from the Sent mailbox.
  Every `--live` run invokes the verifier automatically after sending and
  records `verified` / `expected` / `observed` / `method` in the evidence
  file (a verifier failure is recorded as `verified: false` with an
  `UNVERIFIED: ...` error, never a silent skip). Dry runs skip verification
  because nothing was sent.

## Rollback

- Stop future runs: `systemctl disable --now robie-4359-policy-change.timer`
- Re-pin `/opt/streetsmart-hermes/4359-weekly` to the previous release dir.
- The main app pointer is untouched by this worker's deploys.

## Manual dry-run (from a shell on the box)

```bash
cd /opt/streetsmart-hermes/4359-weekly
sudo -u streetsmart-hermes /opt/streetsmart-hermes/venv/bin/python \
  -m robie_job_engine.run_overdue_policy_change_reports \
  --manifest /etc/streetsmart-hermes/robie-4359-manifest.json \
  --evidence-out /tmp/4359-dryrun.json
```
