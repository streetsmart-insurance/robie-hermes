#!/usr/bin/env bash
set -euo pipefail

archive="${1:?usage: verify-release.sh RELEASE.tgz RELEASE.tgz.sha256}"
checksum="${2:?usage: verify-release.sh RELEASE.tgz RELEASE.tgz.sha256}"

archive_dir="$(cd "$(dirname "${archive}")" && pwd)"
archive_name="$(basename "${archive}")"
checksum_name="$(basename "${checksum}")"
temp_dir="$(mktemp -d)"
cleanup() { rm -rf "${temp_dir}"; }
trap cleanup EXIT

cd "${archive_dir}"
shasum -a 256 -c "${checksum_name}"
tar -xzf "${archive_name}" -C "${temp_dir}"
release_root="$(find "${temp_dir}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
test -n "${release_root}"
cd "${release_root}"
PYTHONPYCACHEPREFIX="${temp_dir}/pycache" python3 -m compileall -q robie_job_engine tests
PYTHONPATH=. python3 -m unittest discover -s tests -v
