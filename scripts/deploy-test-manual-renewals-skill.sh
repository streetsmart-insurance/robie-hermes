#!/usr/bin/env bash
# Atomically installs one immutable Manual Renewals Skill copy on Test only.
set -euo pipefail

EXPECTED_HOST="hermes-test-01"
OPT_ROOT="/opt/streetsmart-hermes-test"
SKILL_NAME="ezlynx-manual-renewals"

source_dir=""
commit=""
expected_digest=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-dir) source_dir="$2"; shift 2 ;;
    --commit) commit="$2"; shift 2 ;;
    --expected-digest) expected_digest="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "Test Skill deploy requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || {
  echo "refusing non-Test host: $(hostname -f)" >&2
  exit 2
}
[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit SHA" >&2; exit 2; }
[[ "${expected_digest}" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid expected digest" >&2; exit 2; }
[[ -f "${source_dir}/SKILL.md" ]] || { echo "staged SKILL.md missing" >&2; exit 2; }

actual_digest="$(sha256sum "${source_dir}/SKILL.md" | awk '{print $1}')"
[[ "${actual_digest}" == "${expected_digest}" ]] || {
  echo "staged Skill digest mismatch" >&2
  exit 2
}

python3 - "${source_dir}/SKILL.md" <<'PY'
from pathlib import Path
import sys

content = Path(sys.argv[1]).read_text(encoding="utf-8")
assert 'name: "ezlynx-manual-renewals"' in content
assert 'job_type: "manual_renewal_verification"' in content
assert 'status: "Testing"' in content
assert "production_ready: false" in content
assert "Manual only" in content
assert "ROBIE was here" in content
assert "Do not close until" in content
PY

release_parent="${OPT_ROOT}/skill-releases/${commit}"
release_dir="${release_parent}/${SKILL_NAME}"
skill_link="${OPT_ROOT}/.hermes/skills/${SKILL_NAME}"
old_target=""
if [[ -e "${skill_link}" || -L "${skill_link}" ]]; then
  [[ -L "${skill_link}" ]] || {
    echo "existing Test Skill destination is not an atomic symlink" >&2
    exit 2
  }
  old_target="$(readlink -f "${skill_link}")"
fi

if [[ -d "${release_dir}" ]]; then
  installed_digest="$(sha256sum "${release_dir}/SKILL.md" | awk '{print $1}')"
  [[ "${installed_digest}" == "${expected_digest}" ]] || {
    echo "existing immutable Test Skill release has different bytes" >&2
    exit 2
  }
else
  staging="${release_parent}/.${SKILL_NAME}.staging-$$"
  trap 'rm -rf -- "${staging:-}"' EXIT
  install -d -m 0755 "${staging}"
  install -m 0644 "${source_dir}/SKILL.md" "${staging}/SKILL.md"
  install -d -m 0755 "${release_parent}"
  mv "${staging}" "${release_dir}"
  trap - EXIT
fi

install -d -m 0755 "$(dirname "${skill_link}")"
python3 - "${release_dir}" "${skill_link}" <<'PY'
import os
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve(strict=True)
link = Path(sys.argv[2])
temporary = link.with_name(link.name + ".new")
try:
    temporary.unlink()
except FileNotFoundError:
    pass
temporary.symlink_to(target)
os.replace(temporary, link)
PY

verified_target="$(readlink -f "${skill_link}")"
verified_digest="$(sha256sum "${skill_link}/SKILL.md" | awk '{print $1}')"
[[ "${verified_target}" == "${release_dir}" ]] || { echo "Test Skill pointer mismatch" >&2; exit 2; }
[[ "${verified_digest}" == "${expected_digest}" ]] || { echo "Test Skill read-back mismatch" >&2; exit 2; }

evidence_dir="${OPT_ROOT}/skill-deployments"
install -d -m 0755 "${evidence_dir}"
python3 - "${evidence_dir}/${commit}-${SKILL_NAME}.json" \
  "${commit}" "${expected_digest}" "${release_dir}" "${skill_link}" "${old_target}" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

payload = {
    "environment": "Test",
    "host": "hermes-test-01",
    "skill": "ezlynx-manual-renewals",
    "job_type": "manual_renewal_verification",
    "state": "Testing",
    "production_ready": False,
    "commit": sys.argv[2],
    "skill_sha256": sys.argv[3],
    "release_dir": sys.argv[4],
    "destination": sys.argv[5],
    "previous_target": sys.argv[6] or None,
    "verified_at": datetime.now(timezone.utc).isoformat(),
    "production_touched": False,
    "customer_record_changed": False,
    "email_sent": False,
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, sort_keys=True))
PY

echo "TEST_SKILL_VERIFIED commit=${commit} skill_sha256=${expected_digest} previous=${old_target:-none}"
