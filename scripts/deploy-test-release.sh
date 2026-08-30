#!/usr/bin/env bash
# Test-only immutable release installer. Refuses every host except hermes-test-01.
set -euo pipefail

OPT_ROOT="/opt/streetsmart-hermes-test"
JOB_DB="${OPT_ROOT}/robie-job-engine/data/jobs.db"
GATEWAY_UNIT="robie-gateway"
EXPECTED_HOST="hermes-test-01"

archive=""
checksum=""
commit=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) archive="$2"; shift 2 ;;
    --checksum) checksum="$2"; shift 2 ;;
    --commit) commit="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "deploy-test-release requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || {
  echo "refusing non-Test host: $(hostname -f)" >&2
  exit 2
}
[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit SHA" >&2; exit 2; }
[[ -f "${archive}" && -f "${checksum}" ]] || { echo "archive/checksum missing" >&2; exit 2; }

short="${commit:0:12}"
expected_archive="robie-hermes-${short}.tgz"
[[ "$(basename "${archive}")" == "${expected_archive}" ]] || {
  echo "archive name does not match commit: ${archive}" >&2
  exit 2
}

archive_dir="$(cd "$(dirname "${archive}")" && pwd)"
(
  cd "${archive_dir}"
  sha256sum -c "$(basename "${checksum}")"
)
archive_digest="$(sha256sum "${archive}" | awk '{print $1}')"

old_current="$(readlink -f "${OPT_ROOT}/current")"
old_releases_current="$(readlink -f "${OPT_ROOT}/releases/current")"
policy_skill_link="${OPT_ROOT}/.hermes/skills/ezlynx-policy-setup"
old_policy_skill_target=""
if [[ -e "${policy_skill_link}" || -L "${policy_skill_link}" ]]; then
  [[ -L "${policy_skill_link}" ]] || {
    echo "existing Test Policy Setup skill is not an atomic symlink; refuse deploy" >&2
    exit 2
  }
  old_policy_skill_target="$(readlink -f "${policy_skill_link}")"
fi
[[ -n "${old_current}" && "${old_current}" == "${old_releases_current}" ]] || {
  echo "Test rollback pointers are missing or disagree" >&2
  exit 2
}

inventory="$(python3 - "${JOB_DB}" <<'PY'
import json
import sqlite3
import sys

path = sys.argv[1]
if not __import__("pathlib").Path(path).is_file():
    print(json.dumps({"blocking": 0, "rows": [], "database": "missing"}))
    raise SystemExit(0)
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    """SELECT id,status,lease_owner,lease_expires_at,updated_at
       FROM jobs
       WHERE status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL
       ORDER BY updated_at DESC"""
).fetchall()
print(json.dumps({"blocking": len(rows), "rows": [dict(row) for row in rows]}))
con.close()
PY
)"
printf 'test_job_inventory=%s\n' "${inventory}"
python3 - "${inventory}" <<'PY'
import json
import sys
data = json.loads(sys.argv[1])
if data.get("database") == "missing":
    raise SystemExit("Test jobs.db missing; refuse deploy without inventory")
if int(data.get("blocking", 0)):
    raise SystemExit("active Test jobs or leases exist; refuse deploy")
PY

release_parent="${OPT_ROOT}/releases/${short}"
release_root="${release_parent}/robie-hermes-${short}"
manifest="${release_root}/.release-sha256"
if [[ -d "${release_root}" ]]; then
  [[ -f "${manifest}" && "$(tr -d '[:space:]' <"${manifest}")" == "${archive_digest}" ]] || {
    echo "existing Test release does not match archive digest" >&2
    exit 2
  }
else
  staging="${OPT_ROOT}/releases/.staging-${short}-$$"
  trap 'rm -rf "${staging:-}"' EXIT
  mkdir -p "${staging}" "${release_parent}"
  tar -xzf "${archive}" -C "${staging}"
  [[ -d "${staging}/robie-hermes-${short}" ]] || {
    echo "release archive root mismatch" >&2
    exit 2
  }
  mv "${staging}/robie-hermes-${short}" "${release_root}"
  printf '%s\n' "${archive_digest}" >"${manifest}"
  rm -rf "${staging}"
  trap - EXIT
fi

# Verify the immutable candidate with the candidate's own tests. Using the
# previous release's verifier prevents a regression-battery fix from ever
# bootstrapping into Test. This still occurs before either pointer is changed
# or the gateway is restarted, and Bash keeps the gate independent of mode
# bits in historical archives.
bash "${release_root}/scripts/verify-release.sh" "${archive}" "${checksum}"

source "${release_root}/scripts/lib/test-release-rollback.sh"

rollback_test() {
  echo "Test verification failed; restoring ${old_current}" >&2
  rollback_test_release \
    "${old_current}" \
    "${old_releases_current}" \
    "${old_policy_skill_target}" \
    "${OPT_ROOT}/current" \
    "${OPT_ROOT}/releases/current" \
    "${policy_skill_link}" \
    "${GATEWAY_UNIT}"
}

before="$(systemctl show "${GATEWAY_UNIT}" -p ActiveEnterTimestamp --value --no-pager)"
set +e
bash "${release_root}/scripts/install-official-release.sh" install \
  --opt-root "${OPT_ROOT}" \
  --release-root "${release_root}" \
  --hermes-home "${OPT_ROOT}/.hermes" \
  --sha "${short}" \
  --db "${JOB_DB}" \
  --gateway-unit "${GATEWAY_UNIT}" \
  --gateway-active-enter "${before}"
install_rc=$?
set -e
[[ "${install_rc}" -eq 2 ]] || {
  echo "pre-restart install returned unexpected status ${install_rc}" >&2
  if [[ "$(readlink -f "${OPT_ROOT}/current")" == "${release_root}" ]]; then
    rollback_test
  fi
  exit 2
}

[[ "$(readlink -f "${OPT_ROOT}/current")" == "${release_root}" ]] || {
  echo "Test current pointer did not flip" >&2
  exit 2
}
[[ "$(readlink -f "${OPT_ROOT}/releases/current")" == "${release_root}" ]] || {
  echo "Test releases/current pointer did not flip" >&2
  rollback_test
  exit 2
}

# systemd's human-readable ActiveEnterTimestamp has whole-second precision,
# while the persisted pointer flip includes microseconds. A restart in the
# same UTC second is therefore impossible to order authoritatively and must
# wait for the next representable systemd timestamp instead of producing a
# false pointer-only failure.
python3 - "${release_root}/official-install-flip.json" <<'PY'
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
flipped = datetime.fromisoformat(str(data["flip_at"]).replace("Z", "+00:00"))
if flipped.tzinfo is None:
    flipped = flipped.replace(tzinfo=timezone.utc)
deadline = time.monotonic() + 3.0
while datetime.now(timezone.utc).replace(microsecond=0) <= flipped:
    if time.monotonic() >= deadline:
        raise SystemExit("could not establish a gateway timestamp after the pointer flip")
    time.sleep(0.05)
PY

# Install only the explicitly Test-only draft skill. The destination is an
# atomic symlink into this immutable release, so rollback restores the exact
# previous skill bytes. Production uses a different root and is never touched.
policy_skill_source="${release_root}/deploy/hermes/skills/ezlynx-policy-setup"
[[ -f "${policy_skill_source}/SKILL.md" ]] || {
  echo "Policy Setup SKILL.md missing from Test candidate" >&2
  rollback_test
  exit 2
}
if ! python3 - "${policy_skill_source}/SKILL.md" \
  "${policy_skill_source}/references/profiles.json" <<'PY'
import json
import pathlib
import sys

skill = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
profiles = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
assert 'version: "0.1.0-draft"' in skill
assert 'status: "Testing"' in skill
assert "production_ready: false" in skill
assert profiles.get("production_ready") is False
assert len(profiles.get("profiles") or []) == 12
assert all(item.get("state") == "Testing" for item in profiles["profiles"])
assert all(item.get("consequential_writes_enabled") is False for item in profiles["profiles"])
PY
then
  echo "Policy Setup Test-only package validation failed" >&2
  rollback_test
  exit 2
fi
mkdir -p "$(dirname "${policy_skill_link}")"
if ! atomic_pointer "${policy_skill_source}" "${policy_skill_link}"; then
  rollback_test
  exit 2
fi
policy_skill_digest="$(sha256sum "${policy_skill_source}/SKILL.md" "${policy_skill_source}/references/profiles.json" | sha256sum | awk '{print $1}')"

if ! systemctl restart "${GATEWAY_UNIT}" || \
  ! systemctl is-active --quiet "${GATEWAY_UNIT}"; then
  rollback_test
  exit 2
fi
after="$(systemctl show "${GATEWAY_UNIT}" -p ActiveEnterTimestamp --value --no-pager)"
if ! bash "${release_root}/scripts/install-official-release.sh" prove \
  --opt-root "${OPT_ROOT}" \
  --release-root "${release_root}" \
  --hermes-home "${OPT_ROOT}/.hermes" \
  --sha "${short}" \
  --db "${JOB_DB}" \
  --gateway-unit "${GATEWAY_UNIT}" \
  --gateway-active-enter "${after}"; then
  rollback_test
  exit 2
fi

proof="${release_root}/official-install-proof.json"
python3 - "${proof}" "${short}" <<'PY'
import json
import pathlib
import sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert data.get("sha") == sys.argv[2]
assert data.get("done") is True
assert data.get("live") is True
assert data.get("authorizes_complete") is False
assert data.get("proof", {}).get("live") is True
PY

evidence_dir="${OPT_ROOT}/deployments/${short}"
mkdir -p "${evidence_dir}"
python3 - "${evidence_dir}/test-deploy-evidence.json" "${inventory}" \
  "${commit}" "${archive_digest}" "${release_root}" "${old_current}" \
  "${GATEWAY_UNIT}" "${after}" "${proof}" "${policy_skill_link}" \
  "${policy_skill_source}" "${policy_skill_digest}" "${old_policy_skill_target}" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone
payload = {
    "environment": "Test",
    "host": "hermes-test-01",
    "commit": sys.argv[3],
    "release_sha256": sys.argv[4],
    "release_root": sys.argv[5],
    "previous_release": sys.argv[6],
    "gateway_unit": sys.argv[7],
    "gateway_active_enter": sys.argv[8],
    "proof_path": sys.argv[9],
    "test_skill": {
        "name": "ezlynx-policy-setup",
        "version": "0.1.0-draft",
        "state": "Testing",
        "destination": sys.argv[10],
        "source": sys.argv[11],
        "profile_manifest": sys.argv[11] + "/references/profiles.json",
        "selector_inventory": sys.argv[11] + "/references/selector-inventory.md",
        "content_digest": sys.argv[12],
        "previous_target": sys.argv[13] or None,
        "consequential_writes_enabled": False,
    },
    "test_job_inventory": json.loads(sys.argv[2]),
    "verified_at": datetime.now(timezone.utc).isoformat(),
    "production_touched": False,
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, sort_keys=True))
PY

echo "TEST VERIFIED commit=${commit} sha256=${archive_digest} rollback=${old_current}"
