#!/usr/bin/env bash
# Install the Applied Pay Wells proof job units and timer from a release tree.
# Does not deploy a release and does not flip /opt/streetsmart-hermes/current.
#
# The job is review-only by construction (no bank actions, QBO posts, EZLynx
# writes, or transfers; every bank-side claim labeled UNVERIFIED until
# WELLS_ACCESS_MODE=live is provisioned). The timer is installed and left
# stopped unless --enable-timer is passed.
#
# Test box, after this commit is already the release on the box:
#   sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
#     --release-dir /opt/streetsmart-hermes-test/current
#
# With the daily timer enabled:
#   sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
#     --release-dir /opt/streetsmart-hermes-test/current --enable-timer
#
# Preview, no writes:
#   scripts/install-applied-pay-proof.sh --dry-run \
#     --release-dir /opt/streetsmart-hermes/current
#
# Rollback (leaves the timer stopped; the old units are in the backup):
#   sudo /opt/streetsmart-hermes-test/current/scripts/install-applied-pay-proof.sh \
#     --rollback /root/robie-applied-pay-proof-YYYYMMDDTHHMMSSZ
#
# Tests pass --prefix and stub --systemctl / --systemd-analyze. --prefix is
# not for Production.
set -euo pipefail

UNIT="robie-applied-pay-proof.service"
QUICK_UNIT="robie-applied-pay-proof-quick.service"
TIMER="robie-applied-pay-proof.timer"
STATE_DIR_DEFAULT="/var/lib/robie-applied-pay-proof"

RELEASE_DIR=""
PREFIX=""
BACKUP_ROOT=""
ROLLBACK=""
ENABLE_TIMER=0
DRY_RUN=0
SYSTEMCTL="${APPLIED_PAY_PROOF_INSTALL_SYSTEMCTL:-systemctl}"
ANALYZE="${APPLIED_PAY_PROOF_INSTALL_ANALYZE:-systemd-analyze}"

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "install-applied-pay-proof: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir) RELEASE_DIR="${2:-}"; shift 2;;
    --prefix) PREFIX="${2:-}"; shift 2;;
    --backup-root) BACKUP_ROOT="${2:-}"; shift 2;;
    --rollback) ROLLBACK="${2:-}"; shift 2;;
    --enable-timer) ENABLE_TIMER=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    --systemctl) SYSTEMCTL="${2:-}"; shift 2;;
    --systemd-analyze) ANALYZE="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) die "unknown argument: $1";;
  esac
done

if [[ -n "$PREFIX" ]]; then
  ETC="${PREFIX}/etc/systemd/system"
  [[ -z "$BACKUP_ROOT" ]] && BACKUP_ROOT="${PREFIX}/var/backups"
  STATE_DIR="${PREFIX}${STATE_DIR_DEFAULT}"
else
  ETC="/etc/systemd/system"
  [[ -z "$BACKUP_ROOT" ]] && BACKUP_ROOT="/root"
  STATE_DIR="${STATE_DIR_DEFAULT}"
fi

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then printf 'dry-run:'; printf ' %q' "$@"; printf '\n'; return 0; fi
  "$@"
}

require_real_root() {
  if [[ "$DRY_RUN" -eq 1 || -n "$PREFIX" ]]; then return 0; fi
  [[ "${EUID}" -eq 0 ]] || die "installer requires sudo (or pass --dry-run / --prefix)"
}

install_file() {
  local src="$1" dest="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then printf 'dry-run: install -m 0644 %q %q\n' "$src" "$dest"; return 0; fi
  if [[ "${EUID}" -eq 0 ]]; then install -o root -g root -m 0644 "$src" "$dest"
  else install -m 0644 "$src" "$dest"; fi
}

backup_tree() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
  dest="${BACKUP_ROOT}/robie-applied-pay-proof-${stamp}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: backup current units and timer to ${dest}"
    echo "BACKUP=${dest}"
    return 0
  fi
  mkdir -p "$dest"
  {
    echo "timer_enabled=$(${SYSTEMCTL} is-enabled "$TIMER" 2>/dev/null || true)"
    echo "timer_active=$(${SYSTEMCTL} is-active "$TIMER" 2>/dev/null || true)"
  } > "${dest}/state.txt"
  for f in "$UNIT" "$QUICK_UNIT" "$TIMER"; do
    [[ -f "${ETC}/${f}" ]] && cp -a "${ETC}/${f}" "${dest}/${f}"
  done
  echo "BACKUP=${dest}"
  printf '%s\n' "$dest"
}

do_rollback() {
  local dest="$1"
  [[ -d "$dest" ]] || die "rollback dir not found: $dest"
  require_real_root
  for f in "$UNIT" "$QUICK_UNIT" "$TIMER"; do
    if [[ -f "${dest}/${f}" ]]; then
      echo "restoring ${f}"
      run_cmd cp -a "${dest}/${f}" "${ETC}/${f}"
    else
      echo "removing ${f} (not present in backup)"
      run_cmd rm -f "${ETC}/${f}"
    fi
  done
  local was_enabled=""
  [[ -f "${dest}/state.txt" ]] && was_enabled="$(grep -E '^timer_enabled=' "${dest}/state.txt" | cut -d= -f2 || true)"
  run_cmd "$SYSTEMCTL" daemon-reload
  if [[ "$was_enabled" == "enabled" ]]; then
    run_cmd "$SYSTEMCTL" enable --now "$TIMER"
  else
    run_cmd "$SYSTEMCTL" disable --now "$TIMER" || true
  fi
  echo "rollback complete from ${dest}; timer left ${was_enabled:-stopped}"
}

if [[ -n "$ROLLBACK" ]]; then do_rollback "$ROLLBACK"; exit 0; fi

[[ -n "$RELEASE_DIR" ]] || die "--release-dir is required"
[[ -d "$RELEASE_DIR" ]] || die "release dir not found: $RELEASE_DIR"
for f in "deploy/systemd/${UNIT}" "deploy/systemd/${QUICK_UNIT}" "deploy/systemd/${TIMER}"; do
  [[ -f "${RELEASE_DIR}/${f}" ]] || die "missing in release: ${f}"
done
[[ -f "${RELEASE_DIR}/applied_pay/wells_proof_job.py" ]] || die "wells_proof_job.py missing in release"
require_real_root

backup_out="$(backup_tree)"
echo "$backup_out" | grep -E '^BACKUP=' || true

mkdir -p "${ETC}"
install_file "${RELEASE_DIR}/deploy/systemd/${UNIT}" "${ETC}/${UNIT}"
install_file "${RELEASE_DIR}/deploy/systemd/${QUICK_UNIT}" "${ETC}/${QUICK_UNIT}"
install_file "${RELEASE_DIR}/deploy/systemd/${TIMER}" "${ETC}/${TIMER}"

run_cmd "$ANALYZE" verify "${ETC}/${UNIT}" "${ETC}/${QUICK_UNIT}" "${ETC}/${TIMER}"
run_cmd "$SYSTEMCTL" daemon-reload

echo "verifying job module loads from the release tree:"
run_cmd "${RELEASE_DIR}/venv/bin/python" -c "import applied_pay.wells_proof_job as m; print('proof job OK:', m.UNVERIFIED)" \
  || run_cmd python3 -c "import sys; sys.path.insert(0, '${RELEASE_DIR}'); import applied_pay.wells_proof_job as m; print('proof job OK:', m.UNVERIFIED)"

if [[ "$ENABLE_TIMER" -eq 1 ]]; then
  run_cmd "$SYSTEMCTL" enable --now "$TIMER"
  echo "timer enabled and started: $TIMER"
else
  run_cmd "$SYSTEMCTL" disable --now "$TIMER" || true
  echo "timer installed but left stopped (pass --enable-timer to start)"
fi
echo "done. State dir: ${STATE_DIR} (created on first run by the unit's ExecStartPre)."
