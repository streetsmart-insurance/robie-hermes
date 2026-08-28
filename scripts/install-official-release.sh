#!/usr/bin/env bash
# Official zip install: the ONLY supported Production flip.
# Long typed SSH commands are not the install path.
#
# This script flips both pointers AND installs every Chat-critical overlay
# (adapter, oauth, Playwright tools, write-guard, gemini helper) from the
# extracted zip. It refuses OFFICIAL INSTALL DONE until live proof holds:
# pointers match, hermes-gateway ActiveEnterTimestamp is after the flip,
# Chat-loaded dests equal that zip, and an install_proof row is written.
# Pointer-only is not live. Chat looking busy is not live.
#
# Skills stay a separate Drive -> .hermes install. This script never writes
# user-owned Loom ascend-finance or any .hermes/skills path.
#
# Does not git pull. Does not bind. Does not print secrets. Does not restart
# hermes-gateway or Chrome. Does not deploy to hermes-poc-01 by being imported.
set -euo pipefail

if [[ "$*" == *git*pull* ]]; then
  echo "refusing git pull; official install consumes an extracted zip only" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
usage:
  install-official-release.sh install --release-root DIR [--opt-root DIR] [--hermes-home DIR] [--sha SHA] [--db JOBS.DB] [--flip-at ISO] [--gateway-active-enter ISO]
  install-official-release.sh prove   --release-root DIR [same flags]

This is the only supported Production zip flip. Do not type a long SSH
cookbook. After OFFICIAL INSTALL DONE:
  pointers match AND Chat dests equal that zip AND hermes-gateway
  ActiveEnterTimestamp is after the flip AND an install_proof row exists.
Pointer-only is not live. Chat looking busy is not COMPLETE.

Operator restarts hermes-gateway after Jake Approves / Carlo Confirms,
then re-runs prove. This script never runs git pull.
EOF
  exit 2
fi

PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONPATH
command="$1"
if [[ "${command}" == "prove" || "${command}" == "install" ]]; then
  shift
  exec python3 -m robie_job_engine.deploy_truth "${command}" "$@"
fi
exec python3 -m robie_job_engine.deploy_truth install "$@"
