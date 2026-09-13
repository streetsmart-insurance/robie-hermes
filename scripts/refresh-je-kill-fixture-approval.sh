#!/usr/bin/env bash
# Owner-only: re-stamp JE-KILL-01 fixture approved_at on hermes-test-01.
# Does not change account/document IDs, locators, or URLs.
# Requires: gcloud auth as Carlo (or Owner) with IAP/SSH to Test.
set -euo pipefail

PROJECT="${ROBIE_SSH_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
VM="${ROBIE_SSH_VM:-hermes-test-01}"
FIXTURE_PATH="${ROBIE_JE_KILL_FIXTURE:-/opt/streetsmart-hermes-test/je-kill/fixture.json}"
APPROVER="${ROBIE_JE_KILL_APPROVER:-Carlo Ferrara}"

if [[ "${VM}" != "hermes-test-01" ]]; then
  echo "refusing: VM must be hermes-test-01" >&2
  exit 2
fi

confirm="${1:-}"
if [[ "${confirm}" != "REAPPROVE_JE_KILL_01_FIXTURE" ]]; then
  echo "usage: $0 REAPPROVE_JE_KILL_01_FIXTURE" >&2
  echo "updates approved_at to now (UTC) for approver=${APPROVER}" >&2
  exit 2
fi

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

gcloud compute ssh "${VM}" \
  --project="${PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
  --command="sudo python3 - <<'PY'
import json
from pathlib import Path
path = Path('${FIXTURE_PATH}')
data = json.loads(path.read_text())
assert data.get('test_only') is True and data.get('disposable') is True
assert data.get('approved_by') == '${APPROVER}'
assert data.get('approval_scope') == 'JE-KILL-01'
data['approved_at'] = '${stamp}'
path.write_text(json.dumps(data, indent=2) + '\n')
print('approved_at', data['approved_at'])
print('approved_by', data['approved_by'])
print('approval_scope', data['approval_scope'])
print('fixture', path)
PY"

echo "JE-KILL fixture re-approved at ${stamp} on ${VM}:${FIXTURE_PATH}"
