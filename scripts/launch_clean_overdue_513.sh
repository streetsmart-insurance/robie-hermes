#!/bin/bash
set -euo pipefail
OUT=/opt/streetsmart-hermes-test/test-tmp/overdue-job-run-513.out
SCRIPT=/opt/streetsmart-hermes-test/test-tmp/run_one_overdue_clean_sheets.py
ENVFILE=/tmp/robie-job-env.full.513

test "$(hostname -s)" = hermes-test-01
test -f "$SCRIPT"
grep -q load_approved_producer_directory "$SCRIPT"
! grep -q map_observed_producers_to_carlo "$SCRIPT"

: > "$ENVFILE"
chmod 600 "$ENVFILE"
systemctl show robie-gateway -p Environment --value \
  | tr ' ' '\n' \
  | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' >> "$ENVFILE" || true
for f in /etc/streetsmart-hermes-test/robie-message-runtime.env \
         /etc/streetsmart-hermes-test/accountability.env; do
  if [ -f "$f" ]; then
    grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$f" >> "$ENVFILE" || true
  fi
done
grep -q '^ROBIE_ENV=' "$ENVFILE" || echo 'ROBIE_ENV=TEST' >> "$ENVFILE"
grep -q '^HOME=' "$ENVFILE" || echo 'HOME=/opt/streetsmart-hermes-test' >> "$ENVFILE"
grep -q '^HERMES_HOME=' "$ENVFILE" || echo 'HERMES_HOME=/opt/streetsmart-hermes-test/.hermes' >> "$ENVFILE"
# force Test sink line present
grep -q '^ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT=' "$ENVFILE" \
  || echo 'ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT=carlo@streetsmart.insurance' >> "$ENVFILE"
chown streetsmart-hermes-test:streetsmart-hermes-test "$ENVFILE"

echo "ENV_KEY_COUNT=$(wc -l < "$ENVFILE")"
echo "SINK_LINE=$(grep ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT "$ENVFILE" || true)"
echo "SCRIPT_SHEETS=$(grep -c load_approved_producer_directory "$SCRIPT" || true)"
echo "RELEASE=$(readlink -f /opt/streetsmart-hermes-test/releases/current)"

rm -f "$OUT"
touch "$OUT"
chown streetsmart-hermes-test:streetsmart-hermes-test "$OUT" "$SCRIPT"
chmod 755 "$SCRIPT"

sudo -u streetsmart-hermes-test bash -c "
set -a
. '$ENVFILE'
set +a
export HOME=/opt/streetsmart-hermes-test
export ROBIE_ENV=TEST
echo START \$(date -u +%Y-%m-%dT%H:%M:%S%z)
echo SINK_PRECHECK=\$ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT
echo HERMES_HOME=\$HERMES_HOME
echo HAS_USER=\$([ -n \"\$ROBIE_EZLYNX_USERNAME_SECRET\" ] && echo yes || echo no)
cd /opt/streetsmart-hermes-test
nohup /opt/streetsmart-hermes-test/venv/bin/python '$SCRIPT' >> '$OUT' 2>&1 &
echo LAUNCHED_PID=\$!
" | tee -a "$OUT"

sleep 8
echo '==== early out ===='
head -40 "$OUT"
echo '==== process ===='
pgrep -af run_one_overdue_clean_sheets || echo NOT_RUNNING
