#!/usr/bin/env bash
# Atomically installs and smoke-tests the complete carrier-proposal Skill on Test only.
set -euo pipefail

EXPECTED_HOST="hermes-test-01"
OPT_ROOT="/opt/streetsmart-hermes-test"
SKILL_NAME="streetsmart-carrier-proposal"

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
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || { echo "refusing non-Test host" >&2; exit 2; }
[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit SHA" >&2; exit 2; }
[[ "${expected_digest}" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid bundle digest" >&2; exit 2; }
[[ -f "${source_dir}/SKILL.md" && -x "${source_dir}/scripts/render_proposal.sh" ]] || {
  echo "staged Skill bundle is incomplete" >&2; exit 2;
}

bundle_digest() {
  python3 - "$1" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

root = Path(sys.argv[1])
digest = sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file() and "node_modules" not in p.parts):
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(sha256(path.read_bytes()).digest())
print(digest.hexdigest())
PY
}

actual_digest="$(bundle_digest "${source_dir}")"
[[ "${actual_digest}" == "${expected_digest}" ]] || { echo "staged bundle digest mismatch" >&2; exit 2; }
command -v node >/dev/null || { echo "node is unavailable on Test" >&2; exit 3; }
command -v npm >/dev/null || command -v pnpm >/dev/null || { echo "npm/pnpm is unavailable on Test" >&2; exit 3; }
command -v soffice >/dev/null || command -v libreoffice >/dev/null || { echo "LibreOffice is unavailable on Test" >&2; exit 3; }
command -v pdfinfo >/dev/null || { echo "pdfinfo is unavailable on Test" >&2; exit 3; }

release_parent="${OPT_ROOT}/skill-releases/${commit}"
release_dir="${release_parent}/${SKILL_NAME}"
skill_link="${OPT_ROOT}/.hermes/skills/${SKILL_NAME}"
old_target=""
if [[ -e "${skill_link}" || -L "${skill_link}" ]]; then
  [[ -L "${skill_link}" ]] || { echo "existing Test destination is not an atomic symlink" >&2; exit 2; }
  old_target="$(readlink -f "${skill_link}")"
fi

if [[ -d "${release_dir}" ]]; then
  [[ "$(bundle_digest "${release_dir}")" == "${expected_digest}" ]] || {
    echo "existing immutable Test release has different bytes" >&2; exit 2;
  }
else
  staging="${release_parent}/.${SKILL_NAME}.staging-$$"
  trap 'rm -rf -- "${staging:-}"' EXIT
  install -d -m 0755 "${release_parent}"
  cp -a "${source_dir}" "${staging}"
  chmod 0755 "${staging}/scripts/render_proposal.sh"
  mv "${staging}" "${release_dir}"
  trap - EXIT
fi

if ! (cd "${release_dir}/scripts" && node -e 'require("docx")' >/dev/null 2>&1); then
  if command -v pnpm >/dev/null 2>&1; then
    (cd "${release_dir}/scripts" && pnpm install --prod --no-frozen-lockfile)
  else
    (cd "${release_dir}/scripts" && npm install --omit=dev --save-exact)
  fi
fi

smoke_dir="$(mktemp -d)"
trap 'rm -rf -- "${smoke_dir:-}"' EXIT
cat >"${smoke_dir}/data.json" <<'JSON'
{"business_name":"Robie Validation Transport LLC","address":"100 Test Route, Newark, NJ 07102","phone":"(973) 555-0100","email":"validation@example.invalid","business_type":"General Freight Trucking","usdot_number":"1234567","policy_period":"09/10/2026 to 09/10/2027","rated_drivers":[{"name":"Test Driver","date_of_birth":"01/01/1980","points":"0","additional_information":""}],"radius_of_operation":"500 miles","coverage_groups":[{"title":"Commercial Automobile Liability","items":[{"name":"Bodily Injury and Property Damage","limit":"$1,000,000 CSL"}]},{"title":"Motor Truck Cargo","items":[{"name":"Cargo Limit","limit":"$100,000 with $1,000 deductible"}]}],"total_annual_premium":"$24,000.00","required_initial_payment":"$4,000.00","taxes_and_fees":"$0.00","required_initial_payment_includes_taxes_and_fees":false,"payment_plan_label":"10 Monthly Payments","monthly_installment":"$2,000.00/month"}
JSON
bash "${release_dir}/scripts/render_proposal.sh" "${smoke_dir}/data.json" "${smoke_dir}/Robie_Validation_Proposal"
[[ -s "${smoke_dir}/Robie_Validation_Proposal.docx" && -s "${smoke_dir}/Robie_Validation_Proposal.pdf" ]] || {
  echo "Test smoke output is incomplete" >&2; exit 4;
}
pages="$(pdfinfo "${smoke_dir}/Robie_Validation_Proposal.pdf" | awk '/^Pages:/ {print $2}')"
[[ "${pages}" -ge 2 ]] || { echo "Test smoke PDF has an invalid page count" >&2; exit 4; }

install -d -m 0755 "$(dirname "${skill_link}")"
python3 - "${release_dir}" "${skill_link}" <<'PY'
import os
from pathlib import Path
import sys
target = Path(sys.argv[1]).resolve(strict=True)
link = Path(sys.argv[2])
temporary = link.with_name(link.name + ".new")
temporary.unlink(missing_ok=True)
temporary.symlink_to(target)
os.replace(temporary, link)
PY

[[ "$(readlink -f "${skill_link}")" == "${release_dir}" ]] || { echo "Test Skill pointer mismatch" >&2; exit 2; }
evidence_dir="${OPT_ROOT}/skill-deployments"
install -d -m 0755 "${evidence_dir}"
python3 - "${evidence_dir}/${commit}-${SKILL_NAME}.json" "${commit}" "${expected_digest}" "${release_dir}" "${skill_link}" "${old_target}" "${pages}" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
payload = {"environment":"Test","host":"hermes-test-01","skill":"streetsmart-carrier-proposal","job_type":"carrier.proposal","commit":sys.argv[2],"bundle_sha256":sys.argv[3],"release_dir":sys.argv[4],"destination":sys.argv[5],"previous_target":sys.argv[6] or None,"smoke_docx":True,"smoke_pdf":True,"smoke_pdf_pages":int(sys.argv[7]),"verified_at":datetime.now(timezone.utc).isoformat(),"production_touched":False}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n", encoding="utf-8")
print(json.dumps(payload, sort_keys=True))
PY
echo "TEST_SKILL_VERIFIED commit=${commit} bundle_sha256=${expected_digest} pdf_pages=${pages}"
