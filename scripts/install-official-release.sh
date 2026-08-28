#!/usr/bin/env bash
# Official zip install: one script flips both pointers AND installs every
# Chat-loaded overlay (adapter / oauth / Playwright tools) from the extracted
# zip, then refuses to call the deploy "done" until proof exists.
#
# Skills stay a separate Drive -> .hermes install. This script never writes
# user-owned Loom ascend-finance or any .hermes/skills path.
#
# Does not git pull. Does not bind. Does not print secrets. Does not restart
# hermes-gateway. Does not deploy to hermes-poc-01 by being imported.
set -euo pipefail

if [[ "$*" == *git*pull* ]]; then
  echo "refusing git pull; official install consumes an extracted zip only" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
usage: install-official-release.sh --release-root DIR [--opt-root DIR] [--hermes-home DIR] [--sha SHA]

Official install is not a zip pointer flip alone. After this script prints
OFFICIAL INSTALL DONE, Chat-loaded dests equal that zip (bytes or zip-load
shim). Pointer-only is not live. hermes-gateway restart and a
gateway_progress row on the next Chat job are still required before
Production is live.

This script never runs git pull.
EOF
  exit 2
fi

PYTHONPATH="${repo_root}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONPATH
exec python3 -m robie_job_engine.deploy_truth install "$@"
