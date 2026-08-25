#!/usr/bin/env bash
set -euo pipefail

archive="${1:?usage: verify-release.sh RELEASE.tgz RELEASE.tgz.sha256}"
checksum="${2:?usage: verify-release.sh RELEASE.tgz RELEASE.tgz.sha256}"

archive_dir="$(cd "$(dirname "${archive}")" && pwd)"
archive_name="$(basename "${archive}")"
checksum_name="$(basename "${checksum}")"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONPATH
work_dir="$(
  python3 -c 'from robie_job_engine.release_verify import durable_verify_workdir; print(durable_verify_workdir())'
)"
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

cd "${archive_dir}"
shasum -a 256 -c "${checksum_name}"
tar -xzf "${archive_name}" -C "${work_dir}"
release_root="$(find "${work_dir}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
test -n "${release_root}"
cd "${release_root}"
PYTHONPYCACHEPREFIX="${work_dir}/pycache" python3 -m compileall -q robie_job_engine tests
PYTHONPATH=. python3 -m unittest discover -s tests -v
