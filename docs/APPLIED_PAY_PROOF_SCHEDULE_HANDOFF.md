# Handoff for Dusty: install the Applied Pay Wells proof job timer (Test box)

**What this is:** systemd units for the scheduled Wells captured-page proof job
(`applied_pay/wells_proof_job.py`, merged in the Applied Pay / Wells stack).
Daily full reconciliation at 06:30 ET + a `--quick` unit the hourly desk sweep
can call. Review-only by construction: no bank actions, no QBO posts, no
EZLynx writes, no transfers. Every bank-side claim is labeled UNVERIFIED until
Carlo's read-only Wells access lands (`WELLS_ACCESS_MODE=live`, currently
refuses to run).

**Do NOT install on Production.** Test box (`hermes-test-01`) only.
Nothing here moves money or posts anything.

## Preconditions

- The release containing `applied_pay/wells_proof_job.py` is already deployed
  to Test and the digest verified (see the deploy report).
- You are on the Test box as a sudoer.

## Install (paste-ready)

```bash
# 1. Preview first — no writes:
sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
  --dry-run --release-dir /opt/streetsmart-hermes-test/current

# 2. Install units + timer, timer LEFT STOPPED:
sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
  --release-dir /opt/streetsmart-hermes-test/current
# Note the BACKUP=... line it prints; you need it for rollback.

# 3. Smoke-run the job once by hand (needs a capture pair + payouts file;
#    use synthetic fixtures — never real bank data for the first run):
sudo -u streetsmart-hermes /opt/streetsmart-hermes/venv/bin/python \
  -m applied_pay.wells_proof_job \
  --capture-dir /var/lib/robie-applied-pay-proof/captures \
  --payouts /var/lib/robie-applied-pay-proof/payouts.json \
  --state /var/lib/robie-applied-pay-proof/state.json \
  --out /opt/streetsmart-hermes/applied-pay/reports/proof-manual.json
```

## Verify (all must pass)

```bash
# Units installed and parse:
systemd-analyze verify /etc/systemd/system/robie-applied-pay-proof.service \
  /etc/systemd/system/robie-applied-pay-proof-quick.service \
  /etc/systemd/system/robie-applied-pay-proof.timer && echo UNITS-OK

# Module loads from the release tree:
sudo -u streetsmart-hermes /opt/streetsmart-hermes/venv/bin/python \
  -c "import applied_pay.wells_proof_job as m; print(m.UNVERIFIED)"
# expected output: UNVERIFIED

# Manual run wrote a report with the UNVERIFIED label and zero write counters:
python3 -c "
import json
r = json.load(open('/opt/streetsmart-hermes/applied-pay/reports/proof-manual.json'))
assert r['verification_label'] == 'UNVERIFIED', r['verification_label']
assert r['bank_actions'] == r['qbo_posts'] == r['ezlynx_writes'] == r['transfers'] == 0
print('REPORT-OK matches=%d unmatched=%d' % (len(r['matches']), len(r['unmatched_payouts'])))
"

# Timer is installed but STOPPED (do not enable until Carlo says so):
systemctl is-enabled robie-applied-pay-proof.timer   # expected: disabled
systemctl is-active robie-applied-pay-proof.timer     # expected: inactive
```

## Enable the daily timer (only on Carlo's explicit go)

```bash
sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
  --release-dir /opt/streetsmart-hermes-test/current --enable-timer
systemctl list-timers robie-applied-pay-proof.timer --all
```

## Rollback

```bash
# Restores the previous units/timer (or removes them if none existed)
# and leaves the timer stopped. Use the BACKUP= path from the install output:
sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
  --rollback /root/robie-applied-pay-proof-YYYYMMDDTHHMMSSZ
systemctl is-enabled robie-applied-pay-proof.timer || echo "timer gone/stopped"
```

## Wiring the hourly desk sweep to --quick

The desk sweep can call the quick unit any time without enabling the timer:

```bash
sudo systemctl start robie-applied-pay-proof-quick.service
```

or run the CLI directly with `--quick` (skips captures already ingested).

## If something is wrong

- `WELLS_ACCESS_MODE=live` in the environment: the job fails closed with
  `ProofError` — expected until Carlo provisions read-only Wells access.
  Do NOT work around it.
- Missing capture pair: the job fails with "missing artifact" / "no captures"
  — the operator must supply `<name>.capture.json` + `<name>.artifact.json`.
- Report back to the main chat with the exact command and output; do not
  improvise fixes on the box.
