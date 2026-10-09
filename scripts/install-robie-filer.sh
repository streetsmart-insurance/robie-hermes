#!/usr/bin/env bash
# Install the robie-filer unit, timer, and drop-ins from a release tree.
# Does not deploy a release and does not flip /opt/streetsmart-hermes/current.
#
# The unit stays a dry run. One verification dry run is started after the
# install and its JSON is printed. The timer is left stopped unless
# --enable-timer is passed. Step 1 live mode (30-live.conf: Queue and
# Needs review, compiled allowlist only) is installed only with --live,
# after the verification dry run. Re-running without --live removes the
# live drop-in (it stays in the backup). The step 2 drop-in
# (40-auto-any-applicant.conf) is never installed by this script.
#
# After this commit is the release on the box:
#   sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh \
#     --release-dir /opt/streetsmart-hermes/current
#
# After Carlo's explicit go for step 1 on the 10-minute timer:
#   sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh \
#     --release-dir /opt/streetsmart-hermes/current --live --enable-timer
#
# Preview, no writes:
#   scripts/install-robie-filer.sh --dry-run --release-dir /opt/streetsmart-hermes/current
#
# Rollback (leaves the timer stopped):
#   sudo /opt/streetsmart-hermes/current/scripts/install-robie-filer.sh \
#     --rollback /root/robie-filer-YYYYMMDDTHHMMSSZ
#
# Tests pass --prefix and stub --systemctl / --systemd-analyze. --prefix is
# not for Production.

set -euo pipefail

UNIT="robie-filer.service"
TIMER="robie-filer.timer"
DROPIN_NAME="${UNIT}.d"
LIVE_EXAMPLE="30-live.conf.example"
LIVE_CONF="30-live.conf"
AUTO_CONF="40-auto-any-applicant.conf"

RELEASE_DIR=""
PREFIX=""
BACKUP_ROOT=""
ROLLBACK=""
ENABLE_TIMER=0
DO_LIVE=0
DRY_RUN=0
SYSTEMCTL="${ROBIE_FILER_INSTALL_SYSTEMCTL:-systemctl}"
ANALYZE="${ROBIE_FILER_INSTALL_ANALYZE:-systemd-analyze}"
JOURNALCTL="${ROBIE_FILER_INSTALL_JOURNALCTL:-journalctl}"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "install-robie-filer: $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir) RELEASE_DIR="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --backup-root) BACKUP_ROOT="${2:-}"; shift 2 ;;
    --rollback) ROLLBACK="${2:-}"; shift 2 ;;
    --enable-timer) ENABLE_TIMER=1; shift ;;
    --live) DO_LIVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --systemctl) SYSTEMCTL="${2:-}"; shift 2 ;;
    --systemd-analyze) ANALYZE="${2:-}"; shift 2 ;;
    --journalctl) JOURNALCTL="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ -n "$PREFIX" ]]; then
  ETC="${PREFIX}/etc/systemd/system"
  BACKUP_ROOT="${BACKUP_ROOT:-${PREFIX}/var/backups}"
else
  ETC="/etc/systemd/system"
  BACKUP_ROOT="${BACKUP_ROOT:-/root}"
fi
DROPIN_DIR="${ETC}/${DROPIN_NAME}"

require_real_root() {
  if [[ "$DRY_RUN" -eq 1 || -n "$PREFIX" ]]; then
    return 0
  fi
  [[ "${EUID}" -eq 0 ]] || die "installer requires sudo (or pass --dry-run / --prefix)"
}

install_file() {
  local src="$1" dest="$2"
  if [[ "${EUID}" -eq 0 ]]; then
    install -o root -g root -m 0644 "$src" "$dest"
  else
    install -m 0644 "$src" "$dest"
  fi
}

backup_tree() {
  local dest
  dest="${BACKUP_ROOT}/robie-filer-$(date -u +%Y%m%dT%H%M%S%NZ)"
  mkdir -p "$dest"
  {
    echo "timer_enabled=$(${SYSTEMCTL} is-enabled "$TIMER" 2>/dev/null || true)"
    echo "timer_active=$(${SYSTEMCTL} is-active "$TIMER" 2>/dev/null || true)"
  } > "${dest}/state.txt"
  : > "${dest}/manifest.txt"
  if [[ -f "${ETC}/${UNIT}" ]]; then cp -a "${ETC}/${UNIT}" "${dest}/${UNIT}"; echo unit >> "${dest}/manifest.txt"; fi
  if [[ -f "${ETC}/${TIMER}" ]]; then cp -a "${ETC}/${TIMER}" "${dest}/${TIMER}"; echo timer >> "${dest}/manifest.txt"; fi
  if [[ -d "$DROPIN_DIR" ]]; then cp -a "$DROPIN_DIR" "${dest}/${DROPIN_NAME}"; echo dropins >> "${dest}/manifest.txt"; fi
  printf '%s\n' "$dest"
}

wait_for_service_idle() {
  local _try state
  for _try in $(seq 1 120); do
    state="$(${SYSTEMCTL} is-active "$UNIT" 2>/dev/null || true)"
    if [[ "$state" != "active" && "$state" != "activating" ]]; then
      return 0
    fi
    sleep 5
  done
  die "timed out waiting for ${UNIT} to finish"
}

stop_timer() {
  "${SYSTEMCTL}" stop "$TIMER" || true
  wait_for_service_idle || true
}

restore_from_backup() {
  local backup="$1"
  [[ -d "$backup" ]] || die "backup dir not found: ${backup}"
  [[ -f "${backup}/manifest.txt" ]] || die "backup is missing manifest.txt: ${backup}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: stop ${TIMER}, restore unit, timer, and drop-ins from ${backup}, daemon-reload, leave timer stopped"
    return 0
  fi
  stop_timer
  mkdir -p "$ETC"
  if grep -qx unit "${backup}/manifest.txt"; then install_file "${backup}/${UNIT}" "${ETC}/${UNIT}"; else rm -f "${ETC}/${UNIT}"; fi
  if grep -qx timer "${backup}/manifest.txt"; then install_file "${backup}/${TIMER}" "${ETC}/${TIMER}"; else rm -f "${ETC}/${TIMER}"; fi
  rm -rf "$DROPIN_DIR"
  if grep -qx dropins "${backup}/manifest.txt"; then cp -a "${backup}/${DROPIN_NAME}" "$DROPIN_DIR"; fi
  "${SYSTEMCTL}" daemon-reload
  "${SYSTEMCTL}" disable --now "$TIMER" || true
  "${SYSTEMCTL}" reset-failed "$UNIT" 2>/dev/null || true
  echo "ROLLBACK=${backup}"
  echo "timer left stopped"
}

install_release() {
  [[ -n "$RELEASE_DIR" ]] || die "--release-dir is required"
  local src="${RELEASE_DIR}/deploy/systemd"
  [[ -f "${src}/${UNIT}" ]] || die "unit missing from release: ${src}/${UNIT}"
  [[ -f "${src}/${TIMER}" ]] || die "timer missing from release: ${src}/${TIMER}"
  [[ -f "${src}/${DROPIN_NAME}/${LIVE_EXAMPLE}" ]] || die "live example missing from release"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: backup, then install ${UNIT} and ${TIMER} from ${src}"
    echo "dry-run: remove ${LIVE_CONF} and ${AUTO_CONF} before the verification run"
    echo "dry-run: ${SYSTEMCTL} daemon-reload; ${ANALYZE} verify ${ETC}/${UNIT}"
    echo "dry-run: ${SYSTEMCTL} start ${UNIT} (dry run) and print its output"
    if [[ "$DO_LIVE" -eq 1 ]]; then echo "dry-run: install ${LIVE_CONF} after the dry run"; else echo "dry-run: live drop-in not installed"; fi
    if [[ "$ENABLE_TIMER" -eq 1 ]]; then echo "dry-run: ${SYSTEMCTL} enable --now ${TIMER}"; else echo "dry-run: ${SYSTEMCTL} disable --now ${TIMER}"; fi
    return 0
  fi

  local backup
  backup="$(backup_tree)"
  [[ "$backup" == "$BACKUP_ROOT"/* ]] || die "refusing unexpected backup path: ${backup}"
  echo "BACKUP=${backup}"

  stop_timer
  mkdir -p "$ETC" "$DROPIN_DIR"
  install_file "${src}/${UNIT}" "${ETC}/${UNIT}"
  install_file "${src}/${TIMER}" "${ETC}/${TIMER}"
  # The verification run is always a dry run. Step 2 is never installed here.
  rm -f "${DROPIN_DIR}/${LIVE_CONF}" "${DROPIN_DIR}/${AUTO_CONF}"
  "${SYSTEMCTL}" daemon-reload
  "${ANALYZE}" verify "${ETC}/${UNIT}"

  local since
  since="$(date -u '+%Y-%m-%d %H:%M:%S')"
  if ! "${SYSTEMCTL}" start "$UNIT"; then
    "${JOURNALCTL}" -u "$UNIT" --since "$since" --no-pager -o cat || true
    "${SYSTEMCTL}" disable --now "$TIMER" || true
    # Do not leave a failed unit behind (systemctl --failed, health checks).
    # The journal above keeps the evidence.
    "${SYSTEMCTL}" reset-failed "$UNIT" || true
    die "verification dry run failed; timer left stopped; failed state cleared. Rollback: --rollback ${backup}"
  fi
  echo "--- verification dry run output ---"
  "${JOURNALCTL}" -u "$UNIT" --since "$since" --no-pager -o cat || true
  echo "--- end ---"

  if [[ "$DO_LIVE" -eq 1 ]]; then
    install_file "${src}/${DROPIN_NAME}/${LIVE_EXAMPLE}" "${DROPIN_DIR}/${LIVE_CONF}"
    "${SYSTEMCTL}" daemon-reload
    echo "installed ${LIVE_CONF}: Queue and Needs review are live on the compiled allowlist"
  fi
  if [[ "$ENABLE_TIMER" -eq 1 ]]; then
    "${SYSTEMCTL}" enable --now "$TIMER"
    echo "timer enabled"
  else
    "${SYSTEMCTL}" disable --now "$TIMER" || true
    echo "timer left stopped"
  fi
  echo "ROLLBACK_TARGET=${backup}"
}

require_real_root
if [[ -n "$ROLLBACK" ]]; then
  restore_from_backup "$ROLLBACK"
else
  install_release
fi
