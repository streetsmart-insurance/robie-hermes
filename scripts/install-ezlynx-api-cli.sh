#!/usr/bin/env bash
# Install the shared EZLynx command line (ezlynx-api) from a release tree.
# Does not deploy a release, restart anything, or call EZLynx.
#
# Puts the wrapper at /usr/local/bin/ezlynx-api, creates the state folder
# /var/lib/ezlynx-api-cli (owner streetsmart-hermes, mode 0750; the append-only
# audit.jsonl and the note ledger live there), and writes
# /etc/streetsmart-hermes/ezlynx-api-cli.env with the ROBIE_ENV choice.
# Ends with `ezlynx-api selftest`, which makes no EZLynx or Gemini call.
#
#   sudo /opt/streetsmart-hermes/current/scripts/install-ezlynx-api-cli.sh \
#     --release-dir /opt/streetsmart-hermes/current
#   (Test host: add --robie-env TEST)
# Preview:  scripts/install-ezlynx-api-cli.sh --dry-run --release-dir DIR
# Remove:   sudo scripts/install-ezlynx-api-cli.sh --uninstall
#           (removes the wrapper and env file; the state folder and audit stay)
#
# Tests pass --prefix. --prefix is not for Production.

set -euo pipefail

RUN_AS="streetsmart-hermes"
RELEASE_DIR=""
PREFIX=""
ROBIE_ENV_CHOICE="PRODUCTION"
DRY_RUN=0
UNINSTALL=0

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install-ezlynx-api-cli: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir) RELEASE_DIR="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --robie-env) ROBIE_ENV_CHOICE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$ROBIE_ENV_CHOICE" in PRODUCTION|TEST) ;; *) die "--robie-env must be PRODUCTION or TEST" ;; esac

BIN="${PREFIX}/usr/local/bin/ezlynx-api"
STATE="${PREFIX}/var/lib/ezlynx-api-cli"
CONF_DIR="${PREFIX}/etc/streetsmart-hermes"
CONF="${CONF_DIR}/ezlynx-api-cli.env"

if [[ "$DRY_RUN" -eq 0 && -z "$PREFIX" && "${EUID}" -ne 0 ]]; then
  die "installer requires sudo (or pass --dry-run / --prefix)"
fi

if [[ "$UNINSTALL" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: remove ${BIN} and ${CONF}; keep ${STATE}"
    exit 0
  fi
  rm -f "$BIN" "$CONF"
  echo "removed ${BIN} and ${CONF}; ${STATE} (audit log) kept"
  exit 0
fi

[[ -n "$RELEASE_DIR" ]] || die "--release-dir is required"
SRC="${RELEASE_DIR}/scripts/ezlynx-api"
[[ -f "$SRC" ]] || die "wrapper missing from release: ${SRC}"
bash -n "$SRC" || die "wrapper has a syntax error"
[[ -f "${RELEASE_DIR}/robie_job_engine/ezlynx_api_cli.py" ]] || die "ezlynx_api_cli.py missing from release"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: install ${SRC} as ${BIN} (root:root 0755)"
  echo "dry-run: create ${STATE} (${RUN_AS}, 0750)"
  echo "dry-run: write ${CONF} with ROBIE_ENV=${ROBIE_ENV_CHOICE}"
  echo "dry-run: run ezlynx-api selftest (no EZLynx or Gemini call)"
  exit 0
fi

mkdir -p "$(dirname "$BIN")" "$CONF_DIR" "$STATE"
if [[ "${EUID}" -eq 0 ]]; then
  install -o root -g root -m 0755 "$SRC" "$BIN"
  if id "$RUN_AS" >/dev/null 2>&1; then
    chown "${RUN_AS}:${RUN_AS}" "$STATE"
  elif [[ -z "$PREFIX" ]]; then
    die "user ${RUN_AS} does not exist on this host"
  fi
else
  install -m 0755 "$SRC" "$BIN"
fi
chmod 0750 "$STATE"

printf '# written by install-ezlynx-api-cli.sh. Secret references only, no secret values.\nROBIE_ENV=%s\n' "$ROBIE_ENV_CHOICE" > "$CONF"
chmod 0644 "$CONF"

echo "installed ${BIN}"
echo "state folder ${STATE}"
echo "ROBIE_ENV=${ROBIE_ENV_CHOICE} in ${CONF}"

if [[ -z "$PREFIX" ]]; then
  echo "--- selftest ---"
  "$BIN" --agent installer selftest
  echo "--- end ---"
fi
