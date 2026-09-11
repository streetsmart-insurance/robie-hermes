#!/usr/bin/env bash
# Build a deterministic, data-free release from an exact legacy server commit.
set -euo pipefail

source_root="${1:?source checkout required}"
commit="${2:?exact legacy commit required}"
output_dir="${3:?output directory required}"

[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid legacy commit" >&2; exit 2; }
test "$(git -C "${source_root}" rev-parse HEAD)" = "${commit}"

short="${commit:0:12}"
archive="${output_dir}/robie-legacy-mailbox-${short}.tgz"
checksum="${archive}.sha256"
mkdir -p "${output_dir}"

git -C "${source_root}" archive \
  --format=tar.gz \
  --prefix="robie-legacy-mailbox-${short}/" \
  --output="${archive}" \
  "${commit}" \
  pyproject.toml requirements.txt src \
  scripts/run_daily_robie_cleaner.sh \
  tests/test_robie_inbox_cleaner.py tests/test_uw_reply_filer.py

if tar -tzf "${archive}" | grep -E \
  '(^|/)(\.env|[^/]*\.pem|[^/]*\.key|[^/]*\.db|[^/]*\.sqlite[^/]*|[^/]*credentials[^/]*\.json|[^/]*token[^/]*\.json)$' \
  >/dev/null; then
  echo "legacy mailbox artifact contains a forbidden sensitive/runtime path" >&2
  exit 1
fi

digest="$(sha256sum "${archive}" | awk '{print $1}')"
printf '%s  %s\n' "${digest}" "$(basename "${archive}")" >"${checksum}"
printf 'legacy_commit=%s\nlegacy_sha256=%s\nlegacy_archive=%s\n' \
  "${commit}" "${digest}" "${archive}"
