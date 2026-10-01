#!/usr/bin/env bash
# Production-only immutable release installer. Refuses every host except hermes-poc-01.
set -euo pipefail

OPT_ROOT="/opt/streetsmart-hermes"
JOB_DB="${OPT_ROOT}/robie-job-engine/data/jobs.db"
GATEWAY_UNIT="hermes-gateway"
EXPECTED_PROJECT="streetsmart-hermes-poc"
EXPECTED_ZONE="us-east1-b"
EXPECTED_HOST="hermes-poc-01"

archive=""
checksum=""
commit=""
expected_digest=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) archive="$2"; shift 2 ;;
    --checksum) checksum="$2"; shift 2 ;;
    --commit) commit="$2"; shift 2 ;;
    --digest) expected_digest="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "Production deploy requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || {
  echo "refusing non-Production host: $(hostname -f)" >&2
  exit 2
}
metadata_url="http://metadata.google.internal/computeMetadata/v1"
metadata_header="Metadata-Flavor: Google"
project="$(curl -fsS -H "${metadata_header}" "${metadata_url}/project/project-id")"
zone="$(curl -fsS -H "${metadata_header}" "${metadata_url}/instance/zone")"
name="$(curl -fsS -H "${metadata_header}" "${metadata_url}/instance/name")"
[[ "${project}" == "${EXPECTED_PROJECT}" ]] || { echo "refusing project ${project}" >&2; exit 2; }
[[ "${zone##*/}" == "${EXPECTED_ZONE}" ]] || { echo "refusing zone ${zone}" >&2; exit 2; }
[[ "${name}" == "${EXPECTED_HOST}" ]] || { echo "refusing instance ${name}" >&2; exit 2; }
[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit SHA" >&2; exit 2; }
[[ "${expected_digest}" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid SHA-256" >&2; exit 2; }
[[ -f "${archive}" && -f "${checksum}" ]] || { echo "archive/checksum missing" >&2; exit 2; }

short="${commit:0:12}"
expected_archive="robie-hermes-${short}.tgz"
[[ "$(basename "${archive}")" == "${expected_archive}" ]] || {
  echo "archive name does not match exact commit" >&2
  exit 2
}
archive_dir="$(cd "$(dirname "${archive}")" && pwd)"
(
  cd "${archive_dir}"
  sha256sum -c "$(basename "${checksum}")"
)
archive_digest="$(sha256sum "${archive}" | awk '{print $1}')"
[[ "${archive_digest}" == "${expected_digest}" ]] || {
  echo "artifact digest does not match approved SHA-256" >&2
  exit 2
}

old_current="$(readlink -f "${OPT_ROOT}/current")"
old_releases_current="$(readlink -f "${OPT_ROOT}/releases/current")"
[[ -n "${old_current}" && "${old_current}" == "${old_releases_current}" ]] || {
  echo "Production rollback pointers are missing or disagree" >&2
  exit 2
}

inventory="$(python3 - "${JOB_DB}" <<'PY'
import json
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file():
    raise SystemExit("Production jobs.db missing; refuse deploy without inventory")
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
printf 'production_job_inventory=%s\n' "${inventory}"
python3 - "${inventory}" <<'PY'
import json
import sys
data = json.loads(sys.argv[1])
if int(data.get("blocking", 0)):
    raise SystemExit("active Production jobs or leases exist; refuse deploy")
PY

release_parent="${OPT_ROOT}/releases/${short}"
release_root="${release_parent}/robie-hermes-${short}"
manifest="${release_root}/.release-sha256"
if [[ -d "${release_root}" ]]; then
  [[ -f "${manifest}" && "$(tr -d '[:space:]' <"${manifest}")" == "${archive_digest}" ]] || {
    echo "existing Production release does not match approved digest" >&2
    exit 2
  }
else
  staging="${OPT_ROOT}/releases/.staging-${short}-$$"
  trap 'rm -rf -- "${staging:-}"' EXIT
  mkdir -p "${staging}" "${release_parent}"
  tar -xzf "${archive}" -C "${staging}"
  [[ -d "${staging}/robie-hermes-${short}" ]] || {
    echo "release archive root mismatch" >&2
    exit 2
  }
  mv "${staging}/robie-hermes-${short}" "${release_root}"
  printf '%s\n' "${archive_digest}" >"${manifest}"
  rm -rf -- "${staging}"
  trap - EXIT
fi

bash "${release_root}/scripts/verify-release.sh" "${archive}" "${checksum}"

applicant_verification="$(PYTHONPATH="${release_root}" python3 - <<'PY'
import json
from robie_job_engine.ezlynx_write_scope import (
    ALLOWED_EZLYNX_WRITE_APPLICANT_IDS,
    EZLYNX_WRITE_SCOPE_REFUSED,
    ezlynx_control_scope_block_reason,
    require_allowed_ezlynx_write_applicant,
    requested_message_applicant,
    write_allowlist_is_unrestricted,
)

allowed = "220250093"
# Unset or empty ROBIE_EZLYNX_WRITE_APPLICANT_IDS allows only test account
# 220250093. A comma list restricts writes to those ids. All clients requires
# ROBIE_EZLYNX_WRITE_SCOPE=all plus ROBIE_PLAYGROUND=1 and its guardrails
# (hard blocks, read-back-then-go, and the undo log). This proof records the
# mode actually compiled into the release process environment; it does not
# perform a live EZLynx write.
if write_allowlist_is_unrestricted():
    assert ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is None
    assert require_allowed_ezlynx_write_applicant(allowed) == allowed
    assert require_allowed_ezlynx_write_applicant("220250094") == "220250094"
    refused_values = (None, "", "SANITIZED-001")
    allowlist_mode = "unrestricted"
    compiled_allowlist = None
else:
    assert allowed in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS
    assert require_allowed_ezlynx_write_applicant(allowed) == allowed
    refused_values = (None, "", "SANITIZED-001")
    if "220250094" not in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS:
        refused_values += ("220250094",)
    allowlist_mode = "restricted"
    compiled_allowlist = sorted(ALLOWED_EZLYNX_WRITE_APPLICANT_IDS)
for value in refused_values:
    try:
        require_allowed_ezlynx_write_applicant(value)
    except RuntimeError as exc:
        assert EZLYNX_WRITE_SCOPE_REFUSED in str(exc)
    else:
        raise AssertionError(f"invalid or non-allowlisted applicant accepted: {value!r}")
wrong_page = ezlynx_control_scope_block_reason(
    "https://app.ezlynx.com/web/account/220250094/policies",
    requested_applicant_id=allowed,
)
unscoped_page = ezlynx_control_scope_block_reason(
    "https://app.ezlynx.com/web/policies",
    requested_applicant_id=allowed,
)
assert wrong_page and EZLYNX_WRITE_SCOPE_REFUSED in wrong_page
assert unscoped_page and EZLYNX_WRITE_SCOPE_REFUSED in unscoped_page
assert requested_message_applicant({"text": "Work on https://app.ezlynx.com/web/account/440000001/overview"}) == "440000001"
assert requested_message_applicant({"text": "applicant 440000001 and applicant 440000002"}) is None
assert requested_message_applicant({"text": "applicant 440000001", "applicant_id": "440000002"}) is None
print(json.dumps({
    "allowlist_mode": allowlist_mode,
    "compiled_allowlist": compiled_allowlist,
    "test_applicant_220250093_allowed": True,
    "invalid_applicant_ids_refused": True,
    "production_scope_requires_active_original_message": True,
    "wrong_page_refused": True,
    "unscoped_page_refused": True,
    "live_ezlynx_write_performed": False,
}, sort_keys=True))
PY
)"

policy_skill_attempt="${commit}-$$"
rollback_started=false
rollback_release() {
  if [[ "${rollback_started}" == true ]]; then
    return
  fi
  rollback_started=true
  echo "Production verification failed; restoring ${old_current}" >&2
  python3 - "${old_current}" "${old_releases_current}" \
    "${OPT_ROOT}/current" "${OPT_ROOT}/releases/current" <<'PY'
import pathlib
import sys
for target, raw_link in ((sys.argv[1], sys.argv[3]), (sys.argv[2], sys.argv[4])):
    link = pathlib.Path(raw_link)
    tmp = link.with_name(link.name + ".rollback-new")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(pathlib.Path(target))
    tmp.replace(link)
PY
  local rollback_failed=0
  PYTHONPATH="${release_root}" python3 -m robie_job_engine.policy_skill_release restore "${OPT_ROOT}" "${release_root}" "${policy_skill_attempt}" || rollback_failed=1
  # Always try to restart the old release even if restoring its skill failed.
  systemctl restart "${GATEWAY_UNIT}" || rollback_failed=1
  systemctl is-active --quiet "${GATEWAY_UNIT}" || rollback_failed=1
  return "${rollback_failed}"
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
  echo "pre-restart official install returned unexpected status ${install_rc}" >&2
  if [[ "$(readlink -f "${OPT_ROOT}/current")" == "${release_root}" ]]; then
    rollback_release
  fi
  exit 2
}

[[ "$(readlink -f "${OPT_ROOT}/current")" == "${release_root}" ]] || exit 2
[[ "$(readlink -f "${OPT_ROOT}/releases/current")" == "${release_root}" ]] || {
  rollback_release
  exit 2
}

python3 - "${release_root}/official-install-flip.json" <<'PY'
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
flipped = datetime.fromisoformat(str(data["flip_at"]).replace("Z", "+00:00"))
if flipped.tzinfo is None:
    flipped = flipped.replace(tzinfo=timezone.utc)
deadline = time.monotonic() + 3.0
while datetime.now(timezone.utc).replace(microsecond=0) <= flipped:
    if time.monotonic() >= deadline:
        raise SystemExit("could not establish a gateway timestamp after the pointer flip")
    time.sleep(0.05)
PY

# This separately reviewed skill install preserves the previous directory/link.
# It does not modify unrelated user-owned skills.
if ! PYTHONPATH="${release_root}" python3 -m robie_job_engine.policy_skill_release install "${OPT_ROOT}" "${release_root}" "${policy_skill_attempt}"; then
  rollback_release
  exit 2
fi

if ! systemctl restart "${GATEWAY_UNIT}" || ! systemctl is-active --quiet "${GATEWAY_UNIT}"; then
  rollback_release
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
  rollback_release
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
python3 - "${evidence_dir}/production-deploy-evidence.json" "${inventory}" \
  "${commit}" "${archive_digest}" "${release_root}" "${old_current}" \
  "${GATEWAY_UNIT}" "${after}" "${proof}" "${applicant_verification}" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

payload = {
    "environment": "Production",
    "project": "streetsmart-hermes-poc",
    "zone": "us-east1-b",
    "host": "hermes-poc-01",
    "commit": sys.argv[3],
    "release_sha256": sys.argv[4],
    "release_root": sys.argv[5],
    "rollback_target": sys.argv[6],
    "gateway": {
        "unit": sys.argv[7],
        "active": True,
        "active_enter": sys.argv[8],
    },
    "official_install_proof": sys.argv[9],
    "predeploy_job_inventory": json.loads(sys.argv[2]),
    "ezlynx_write_scope": json.loads(sys.argv[10]),
    "verified_at": datetime.now(timezone.utc).isoformat(),
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, sort_keys=True))
PY

echo "PRODUCTION VERIFIED commit=${commit} sha256=${archive_digest} rollback=${old_current}"
