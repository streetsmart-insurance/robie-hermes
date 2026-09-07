#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
output_dir="${1:-${repo_root}/dist}"
commit_sha="$(git -C "${repo_root}" rev-parse HEAD)"
short_sha="$(git -C "${repo_root}" rev-parse --short=12 HEAD)"
archive="${output_dir}/robie-hermes-${short_sha}.tgz"
checksum="${archive}.sha256"

mkdir -p "${output_dir}"
git -C "${repo_root}" archive \
  --format=tar.gz \
  --prefix="robie-hermes-${short_sha}/" \
  --output="${archive}" \
  "${commit_sha}"

if tar -tzf "${archive}" | grep -E \
  '(^|/)(\.env|[^/]*\.pem|[^/]*\.key|[^/]*\.db|[^/]*\.sqlite[^/]*|[^/]*credentials[^/]*\.json|[^/]*token[^/]*\.json)$' \
  >/dev/null; then
  echo "Release archive contains a forbidden sensitive/runtime path" >&2
  exit 1
fi

archive_sha="$(shasum -a 256 "${archive}" | awk '{print $1}')"
printf '%s  %s\n' "${archive_sha}" "$(basename "${archive}")" > "${checksum}"
printf 'release_commit=%s\nrelease_sha256=%s\nrelease_archive=%s\n' \
  "${commit_sha}" "${archive_sha}" "${archive}"
