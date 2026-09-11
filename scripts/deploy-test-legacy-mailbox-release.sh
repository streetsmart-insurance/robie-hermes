#!/usr/bin/env bash
# Test-only legacy mailbox installer. It never reads live Gmail/EZLynx credentials.
set -euo pipefail

EXPECTED_HOST="hermes-test-01"
OPT_ROOT="/opt/renewal-automation-system-test"

candidate_archive=""
candidate_checksum=""
candidate_commit=""
candidate_digest=""
baseline_archive=""
baseline_checksum=""
baseline_commit=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --candidate-archive) candidate_archive="$2"; shift 2 ;;
    --candidate-checksum) candidate_checksum="$2"; shift 2 ;;
    --candidate-commit) candidate_commit="$2"; shift 2 ;;
    --candidate-digest) candidate_digest="$2"; shift 2 ;;
    --baseline-archive) baseline_archive="$2"; shift 2 ;;
    --baseline-checksum) baseline_checksum="$2"; shift 2 ;;
    --baseline-commit) baseline_commit="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "Test installer requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || { echo "refusing non-Test host" >&2; exit 2; }
[[ "${candidate_commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid candidate commit" >&2; exit 2; }
[[ "${baseline_commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid baseline commit" >&2; exit 2; }
[[ "${candidate_digest}" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid candidate digest" >&2; exit 2; }

install_release() {
  local archive="$1" checksum="$2" commit="$3"
  local short="${commit:0:12}"
  local expected="robie-legacy-mailbox-${short}.tgz"
  local release_parent="${OPT_ROOT}/releases/${short}"
  local release_root="${release_parent}/robie-legacy-mailbox-${short}"
  [[ -f "${archive}" && -f "${checksum}" ]] || { echo "release inputs missing" >&2; exit 2; }
  [[ "$(basename "${archive}")" == "${expected}" ]] || { echo "release name mismatch" >&2; exit 2; }
  (cd "$(dirname "${archive}")" && sha256sum -c "$(basename "${checksum}")" >/dev/null)
  if [[ ! -d "${release_root}" ]]; then
    local staging="${OPT_ROOT}/releases/.staging-${short}-$$"
    mkdir -p "${staging}" "${release_parent}"
    tar -xzf "${archive}" -C "${staging}"
    [[ -d "${staging}/robie-legacy-mailbox-${short}" ]] || { echo "archive root mismatch" >&2; exit 2; }
    mv "${staging}/robie-legacy-mailbox-${short}" "${release_root}"
  fi
  printf '%s\n' "${release_root}"
}

mkdir -p "${OPT_ROOT}/releases" "${OPT_ROOT}/evidence" "${OPT_ROOT}/runtimes"
baseline_root="$(install_release "${baseline_archive}" "${baseline_checksum}" "${baseline_commit}")"
candidate_root="$(install_release "${candidate_archive}" "${candidate_checksum}" "${candidate_commit}")"
test "$(sha256sum "${candidate_archive}" | awk '{print $1}')" = "${candidate_digest}"

old_current=""
if [[ -L "${OPT_ROOT}/current" ]]; then
  old_current="$(readlink -f "${OPT_ROOT}/current")"
fi
rollback_target="${old_current:-${baseline_root}}"
[[ -d "${rollback_target}" ]] || { echo "rollback target missing" >&2; exit 2; }

runtime="${OPT_ROOT}/runtimes/${candidate_digest}"
if [[ ! -x "${runtime}/bin/python" ]]; then
  python3 -m venv "${runtime}"
  "${runtime}/bin/python" -m pip install --disable-pip-version-check --no-input \
    -r "${candidate_root}/requirements.txt" markdown bandit ruff
fi

export PYTHONPATH="${candidate_root}"
"${runtime}/bin/python" -m py_compile \
  "${candidate_root}/src/email_outreach/robie_inbox_cleaner.py" \
  "${candidate_root}/src/email_outreach/uw_reply_filer.py"
"${runtime}/bin/ruff" check --select F,E9 \
  "${candidate_root}/src/email_outreach/robie_inbox_cleaner.py" \
  "${candidate_root}/src/email_outreach/uw_reply_filer.py" \
  "${candidate_root}/tests/test_robie_inbox_cleaner.py" \
  "${candidate_root}/tests/test_uw_reply_filer.py"
"${runtime}/bin/bandit" -q -r \
  "${candidate_root}/src/email_outreach/robie_inbox_cleaner.py" \
  "${candidate_root}/src/email_outreach/uw_reply_filer.py"
junit="${OPT_ROOT}/evidence/${candidate_commit}.junit.xml"
"${runtime}/bin/pytest" -q --junitxml="${junit}" \
  "${candidate_root}/tests/test_robie_inbox_cleaner.py" \
  "${candidate_root}/tests/test_uw_reply_filer.py"

# Atomic Test flip, followed by a real rollback rehearsal and restoration.
ln -sfn "${candidate_root}" "${OPT_ROOT}/current.next"
mv -Tf "${OPT_ROOT}/current.next" "${OPT_ROOT}/current"
test "$(readlink -f "${OPT_ROOT}/current")" = "${candidate_root}"
ln -sfn "${rollback_target}" "${OPT_ROOT}/current.rollback"
mv -Tf "${OPT_ROOT}/current.rollback" "${OPT_ROOT}/current"
test "$(readlink -f "${OPT_ROOT}/current")" = "${rollback_target}"
ln -sfn "${candidate_root}" "${OPT_ROOT}/current.next"
mv -Tf "${OPT_ROOT}/current.next" "${OPT_ROOT}/current"
test "$(readlink -f "${OPT_ROOT}/current")" = "${candidate_root}"

evidence="${OPT_ROOT}/evidence/${candidate_commit}.json"
python3 - "${junit}" "${evidence}" "${candidate_commit}" "${candidate_digest}" \
  "${baseline_commit}" "${rollback_target}" "${candidate_root}" <<'PY'
import datetime
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

junit, output, commit, digest, baseline, rollback, release = sys.argv[1:]
suite = ET.parse(junit).getroot()
data = {
    "schema": "robie.legacy_mailbox_test_evidence.v1",
    "test_verified": True,
    "environment": "TEST",
    "target": "hermes-test-01",
    "legacy_commit": commit,
    "release_sha256": digest,
    "baseline_commit": baseline,
    "release_root": release,
    "rollback_target": rollback,
    "rollback_rehearsed": True,
    "external_writes": False,
    "gmail_ezlynx_mode": "mocked_failure_mode_regressions",
    "tests": int(suite.attrib.get("tests", 0)),
    "failures": int(suite.attrib.get("failures", 0)),
    "errors": int(suite.attrib.get("errors", 0)),
    "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
pathlib.Path(output).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
cat "${evidence}"
