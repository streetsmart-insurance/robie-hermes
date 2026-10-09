#!/usr/bin/env bash
# Install the shared EZLynx command line (ezlynx-api) from a release tree.
# Does not deploy a release, restart anything, or call EZLynx.
#
# Puts the wrapper at /usr/local/bin/ezlynx-api, creates the state folder
# (owner = the host's Hermes service user, mode 0750; the append-only
# audit.jsonl and the note ledger live there), and writes an ezlynx-api-cli.env
# settings file with the ROBIE_ENV choice and the host layout.
# Ends with `ezlynx-api selftest`, which makes no EZLynx or Gemini call.
#
# Two layouts. --robie-env picks one, and the installer refuses a host that
# does not look like it (it will not put the Production layout on the Test
# host, or the other way round):
#   PRODUCTION (default)  user streetsmart-hermes       /opt/streetsmart-hermes
#                         state /var/lib/ezlynx-api-cli
#                         settings /etc/streetsmart-hermes/ezlynx-api-cli.env
#   TEST                  user streetsmart-hermes-test  /opt/streetsmart-hermes-test
#                         state /var/lib/ezlynx-api-cli-test
#                         settings /etc/streetsmart-hermes-test/ezlynx-api-cli.env
#
#   sudo /opt/streetsmart-hermes/current/scripts/install-ezlynx-api-cli.sh \
#     --release-dir /opt/streetsmart-hermes/current
#   Test host: sudo .../install-ezlynx-api-cli.sh --robie-env TEST \
#     --release-dir /opt/streetsmart-hermes-test/releases/current
# Preview:  scripts/install-ezlynx-api-cli.sh --dry-run --release-dir DIR
# Remove:   sudo scripts/install-ezlynx-api-cli.sh --uninstall [--robie-env TEST]
#           (removes the wrapper and settings file; the state folder and audit stay)
#
# Tests pass --prefix. --prefix is not for Production.

set -euo pipefail

RELEASE_DIR=""
PREFIX=""
ROBIE_ENV_CHOICE="PRODUCTION"
DRY_RUN=0
UNINSTALL=0

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }
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

if [[ "$ROBIE_ENV_CHOICE" == "TEST" ]]; then
  RUN_AS="streetsmart-hermes-test"
  HOST_ROOT="/opt/streetsmart-hermes-test"
  OTHER_ROOT="/opt/streetsmart-hermes"
  RELEASE_LINK="${HOST_ROOT}/releases/current"
  STATE_REL="/var/lib/ezlynx-api-cli-test"
  CONF_REL="/etc/streetsmart-hermes-test"
else
  RUN_AS="streetsmart-hermes"
  HOST_ROOT="/opt/streetsmart-hermes"
  OTHER_ROOT="/opt/streetsmart-hermes-test"
  RELEASE_LINK="${HOST_ROOT}/current"
  STATE_REL="/var/lib/ezlynx-api-cli"
  CONF_REL="/etc/streetsmart-hermes"
fi
PYTHON_BIN="${HOST_ROOT}/venv/bin/python"
BIN="${PREFIX}/usr/local/bin/ezlynx-api"
STATE="${PREFIX}${STATE_REL}"
CONF_DIR="${PREFIX}${CONF_REL}"
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

# Refuse a host that does not match the chosen layout.
if [[ ! -d "${PREFIX}${HOST_ROOT}" ]]; then
  if [[ -d "${PREFIX}${OTHER_ROOT}" ]]; then
    die "this host has ${OTHER_ROOT} but not ${HOST_ROOT}. --robie-env ${ROBIE_ENV_CHOICE} is the wrong layout here; use --robie-env $([[ "$ROBIE_ENV_CHOICE" == TEST ]] && echo PRODUCTION || echo TEST)."
  fi
  die "${HOST_ROOT} not found: this host has no ${ROBIE_ENV_CHOICE} Hermes install."
fi
[[ -x "${PREFIX}${PYTHON_BIN}" ]] || die "${PYTHON_BIN} not found: the ${ROBIE_ENV_CHOICE} Hermes virtualenv is missing."
if [[ -z "$PREFIX" ]]; then
  case "$(readlink -f "$RELEASE_DIR")" in
    "${HOST_ROOT}"/*) ;;
    *) die "--release-dir ${RELEASE_DIR} is not under ${HOST_ROOT}. Install from this host's own release." ;;
  esac
fi
SRC="${RELEASE_DIR}/scripts/ezlynx-api"
[[ -f "$SRC" ]] || die "wrapper missing from release: ${SRC}"
bash -n "$SRC" || die "wrapper has a syntax error"
[[ -f "${RELEASE_DIR}/robie_job_engine/ezlynx_api_cli.py" ]] || die "ezlynx_api_cli.py missing from release"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: install ${SRC} as ${BIN} (root:root 0755)"
  echo "dry-run: create ${STATE} (${RUN_AS}, 0750)"
  echo "dry-run: write ${CONF} with ROBIE_ENV=${ROBIE_ENV_CHOICE}, user ${RUN_AS}, release ${RELEASE_LINK}, python ${PYTHON_BIN}"
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

{
  echo "# written by install-ezlynx-api-cli.sh. Secret references only, no secret values."
  echo "ROBIE_ENV=${ROBIE_ENV_CHOICE}"
  echo "EZLYNX_API_RUN_AS=${RUN_AS}"
  echo "EZLYNX_API_RELEASE=${RELEASE_LINK}"
  echo "EZLYNX_API_PYTHON=${PYTHON_BIN}"
  echo "ROBIE_API_CLI_STATE_DIR=${STATE_REL}"
} > "$CONF"
chmod 0644 "$CONF"

echo "installed ${BIN}"
echo "state folder ${STATE}"
echo "ROBIE_ENV=${ROBIE_ENV_CHOICE}, user ${RUN_AS}, release ${RELEASE_LINK} (settings in ${CONF})"

if [[ -z "$PREFIX" ]]; then
  echo "--- selftest ---"
  "$BIN" --agent installer selftest
  echo "--- end ---"
fi
