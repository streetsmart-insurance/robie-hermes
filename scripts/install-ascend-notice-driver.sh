#!/usr/bin/env bash
# Install the Ascend notice driver unit, drop-ins, and timer from a release tree.
# Does not deploy a release and does not flip /opt/streetsmart-hermes/current.
#
# The unit stays a dry run. The timer is installed and left stopped unless
# --enable-timer is passed. Live mode (ASCEND_DRIVER_LIVE=1) is installed
# only with --live, and only after the verification dry run. Re-running
# without --live removes an existing live drop-in (it stays in the backup).
# Re-running without --enable-timer stops the timer.
#
# Prod, after this commit is already the release on the box:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-notice-driver.sh \
#     --release-dir /opt/streetsmart-hermes/current
#
# After Carlo's explicit go for live filing on the 15-minute timer:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-notice-driver.sh \
#     --release-dir /opt/streetsmart-hermes/current \
#     --live --enable-timer
#
# Preview, no writes:
#   scripts/install-ascend-notice-driver.sh --dry-run \
#     --release-dir /opt/streetsmart-hermes/current
#
# Rollback (leaves the timer stopped; the old unit is in the backup):
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-notice-driver.sh \
#     --rollback /root/robie-ascend-notice-driver-YYYYMMDDTHHMMSSZ
#
# Tests pass --prefix and stub --systemctl / --systemd-analyze. --prefix is
# not for Production.
#
# The note ledger the driver locks lives in
# /var/lib/robie-ascend-notice-driver/discussion-note-ledger.json
# (mode 0600, owner streetsmart-hermes). The installer creates that
# directory mode 0755 for the unit user. If
# /opt/streetsmart-hermes/robie-job-engine/data/discussion-note-ledger.json
# has notes and the new file does not, it is copied once. The old file is
# not locked and not written. Root can read it when the driver user cannot.

set -euo pipefail

UNIT="robie-ascend-notice-driver.service"
TIMER="robie-ascend-notice-driver.timer"
DROPIN_NAME="${UNIT}.d"
LIVE_EXAMPLE="30-live.conf.example"
LIVE_CONF="30-live.conf"
WRITE_SCOPE_CONF="10-write-scope.conf"
STATE_DIR_DEFAULT="/var/lib/robie-ascend-notice-driver"
UNIT_USER="streetsmart-hermes"
UNIT_GROUP="streetsmart-hermes"
LEGACY_LEDGER_DEFAULT="/opt/streetsmart-hermes/robie-job-engine/data/discussion-note-ledger.json"

RELEASE_DIR=""
PREFIX=""
BACKUP_ROOT=""
ROLLBACK=""
ENABLE_TIMER=0
DO_LIVE=0
DRY_RUN=0
SYSTEMCTL="${ASCEND_DRIVER_INSTALL_SYSTEMCTL:-systemctl}"
ANALYZE="${ASCEND_DRIVER_INSTALL_ANALYZE:-systemd-analyze}"

usage() {
  sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "install-ascend-notice-driver: $*" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir)
      RELEASE_DIR="${2:-}"
      shift 2
      ;;
    --prefix)
      PREFIX="${2:-}"
      shift 2
      ;;
    --backup-root)
      BACKUP_ROOT="${2:-}"
      shift 2
      ;;
    --rollback)
      ROLLBACK="${2:-}"
      shift 2
      ;;
    --enable-timer)
      ENABLE_TIMER=1
      shift
      ;;
    --live)
      DO_LIVE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --systemctl)
      SYSTEMCTL="${2:-}"
      shift 2
      ;;
    --systemd-analyze)
      ANALYZE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -n "$PREFIX" ]]; then
  ETC="${PREFIX}/etc/systemd/system"
  if [[ -z "$BACKUP_ROOT" ]]; then
    BACKUP_ROOT="${PREFIX}/var/backups"
  fi
  STATE_DIR="${PREFIX}${STATE_DIR_DEFAULT}"
else
  ETC="/etc/systemd/system"
  if [[ -z "$BACKUP_ROOT" ]]; then
    BACKUP_ROOT="/root"
  fi
  STATE_DIR="${STATE_DIR_DEFAULT}"
fi

DROPIN_DIR="${ETC}/${DROPIN_NAME}"

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry-run:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

require_real_root() {
  if [[ "$DRY_RUN" -eq 1 || -n "$PREFIX" ]]; then
    return 0
  fi
  [[ "${EUID}" -eq 0 ]] || die "installer requires sudo (or pass --dry-run / --prefix)"
}

install_file() {
  local src="$1"
  local dest="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry-run: install -m 0644 %q %q\n' "$src" "$dest"
    return 0
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    install -o root -g root -m 0644 "$src" "$dest"
  else
    install -m 0644 "$src" "$dest"
  fi
}

ensure_dir() {
  local dir="$1"
  local mode="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry-run: mkdir -p %q && chmod %s %q\n' "$dir" "$mode" "$dir"
    return 0
  fi
  mkdir -p "$dir"
  chmod "$mode" "$dir"
}

legacy_note_ledger() {
  if [[ -n "${ASCEND_DRIVER_LEGACY_NOTE_LEDGER:-}" ]]; then
    printf '%s\n' "$ASCEND_DRIVER_LEGACY_NOTE_LEDGER"
    return
  fi
  if [[ -n "$PREFIX" ]]; then
    printf '%s\n' "${PREFIX}${LEGACY_LEDGER_DEFAULT}"
  else
    printf '%s\n' "$LEGACY_LEDGER_DEFAULT"
  fi
}

own_state_dir() {
  local dir="$1"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry-run: chown %s:%s %q\n' "$UNIT_USER" "$UNIT_GROUP" "$dir"
    return 0
  fi
  if [[ "${EUID}" -eq 0 ]] && id "$UNIT_USER" >/dev/null 2>&1; then
    chown "${UNIT_USER}:${UNIT_GROUP}" "$dir"
  fi
}

migrate_note_ledger() {
  local legacy dest py status
  legacy="$(legacy_note_ledger)"
  dest="${STATE_DIR}/discussion-note-ledger.json"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'dry-run: migrate note ledger %q -> %q (mode 0600, owner %s)\n' \
      "$legacy" "$dest" "$UNIT_USER"
    return 0
  fi
  py="${ASCEND_DRIVER_INSTALL_PYTHON:-python3}"
  if [[ -z "$PREFIX" && -x /opt/streetsmart-hermes/venv/bin/python ]]; then
    py="/opt/streetsmart-hermes/venv/bin/python"
  fi
  status="$(
    PYTHONPATH="${RELEASE_DIR}${PYTHONPATH:+:$PYTHONPATH}" \
      ASCEND_DRIVER_NOTE_LEDGER="$dest" \
      ASCEND_DRIVER_LEGACY_NOTE_LEDGER="$legacy" \
      "$py" -c 'from robie_job_engine.discussion_note_ledger import migrate_driver_note_ledger; print(migrate_driver_note_ledger())'
  )"
  echo "note ledger migration: ${status}"
  if [[ -f "$dest" ]]; then
    chmod 0600 "$dest"
    if [[ "${EUID}" -eq 0 ]] && id "$UNIT_USER" >/dev/null 2>&1; then
      chown "${UNIT_USER}:${UNIT_GROUP}" "$dest"
    fi
  fi
}

dropin_sets_mailbox() {
  local file="$1"
  grep -E -q '^[[:space:]]*Environment=.*ASCEND_DRIVER_MAILBOX(ES)?=' "$file"
}

dropin_sets_live() {
  local file="$1"
  grep -E -q '^[[:space:]]*Environment=.*ASCEND_DRIVER_LIVE=' "$file"
}

remove_matching_dropins() {
  local kind="$1"
  [[ -d "$DROPIN_DIR" ]] || return 0
  local conf
  shopt -s nullglob
  for conf in "${DROPIN_DIR}/"*.conf; do
    local match=0
    if [[ "$kind" == "mailbox" ]] && dropin_sets_mailbox "$conf"; then
      match=1
    fi
    if [[ "$kind" == "live" ]] && dropin_sets_live "$conf"; then
      match=1
    fi
    if [[ "$match" -eq 1 ]]; then
      echo "removing ${kind} drop-in ${conf}"
      run_cmd rm -f "$conf"
    fi
  done
  shopt -u nullglob
}

backup_tree() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
  dest="${BACKUP_ROOT}/robie-ascend-notice-driver-${stamp}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: backup current unit, drop-ins, and timer to ${dest}"
    echo "BACKUP=${dest}"
    return 0
  fi
  mkdir -p "$dest"
  {
    echo "timer_enabled=$(${SYSTEMCTL} is-enabled "$TIMER" 2>/dev/null || true)"
    echo "timer_active=$(${SYSTEMCTL} is-active "$TIMER" 2>/dev/null || true)"
    echo "service_active=$(${SYSTEMCTL} is-active "$UNIT" 2>/dev/null || true)"
  } > "${dest}/state.txt"
  : > "${dest}/manifest.txt"
  if [[ -f "${ETC}/${UNIT}" ]]; then
    cp -a "${ETC}/${UNIT}" "${dest}/${UNIT}"
    echo "unit" >> "${dest}/manifest.txt"
  fi
  if [[ -f "${ETC}/${TIMER}" ]]; then
    cp -a "${ETC}/${TIMER}" "${dest}/${TIMER}"
    echo "timer" >> "${dest}/manifest.txt"
  fi
  if [[ -d "$DROPIN_DIR" ]]; then
    cp -a "$DROPIN_DIR" "${dest}/${DROPIN_NAME}"
    echo "dropins" >> "${dest}/manifest.txt"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    (
      cd "$dest"
      find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
    )
  fi
  echo "BACKUP=${dest}"
  printf '%s\n' "$dest"
}

wait_for_service_idle() {
  local _try state
  # A live pass over the staff mailboxes can run for several minutes.
  for _try in $(seq 1 60); do
    state="$(${SYSTEMCTL} is-active "$UNIT" 2>/dev/null || true)"
    if [[ "$state" != "active" && "$state" != "activating" ]]; then
      return 0
    fi
    sleep 5
  done
  die "timed out waiting for ${UNIT} to finish"
}

stop_timer() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: ${SYSTEMCTL} stop ${TIMER}"
    return 0
  fi
  "${SYSTEMCTL}" stop "$TIMER" || true
  wait_for_service_idle || true
}

restore_from_backup() {
  local backup="$1"
  [[ -d "$backup" ]] || die "backup dir not found: ${backup}"
  [[ -f "${backup}/manifest.txt" ]] || die "backup is missing manifest.txt: ${backup}"
  echo "restoring ${UNIT} from ${backup}"
  stop_timer
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: restore unit, timer, and drop-ins from ${backup}"
    echo "dry-run: ${SYSTEMCTL} daemon-reload"
    echo "dry-run: ${SYSTEMCTL} disable --now ${TIMER}"
    echo "ROLLBACK=${backup}"
    return 0
  fi
  mkdir -p "$ETC"
  if grep -qx "unit" "${backup}/manifest.txt"; then
    install_file "${backup}/${UNIT}" "${ETC}/${UNIT}"
  else
    rm -f "${ETC}/${UNIT}"
  fi
  if grep -qx "timer" "${backup}/manifest.txt"; then
    install_file "${backup}/${TIMER}" "${ETC}/${TIMER}"
  else
    rm -f "${ETC}/${TIMER}"
  fi
  rm -rf "$DROPIN_DIR"
  if grep -qx "dropins" "${backup}/manifest.txt"; then
    cp -a "${backup}/${DROPIN_NAME}" "$DROPIN_DIR"
  fi
  "${SYSTEMCTL}" daemon-reload
  "${SYSTEMCTL}" disable --now "$TIMER" || true
  echo "ROLLBACK=${backup}"
  echo "timer left stopped; start the previous timer only with an explicit go"
}

install_release() {
  [[ -n "$RELEASE_DIR" ]] || die "--release-dir is required"
  [[ -d "$RELEASE_DIR" ]] || die "release dir not found: ${RELEASE_DIR}"
  local src_unit="${RELEASE_DIR}/deploy/systemd/${UNIT}"
  local src_timer="${RELEASE_DIR}/deploy/systemd/${TIMER}"
  local src_scope="${RELEASE_DIR}/deploy/systemd/${DROPIN_NAME}/${WRITE_SCOPE_CONF}"
  local src_live="${RELEASE_DIR}/deploy/systemd/${DROPIN_NAME}/${LIVE_EXAMPLE}"
  [[ -f "$src_unit" ]] || die "unit missing from release: ${src_unit}"
  [[ -f "$src_timer" ]] || die "timer missing from release: ${src_timer}"
  [[ -f "$src_scope" ]] || die "write-scope drop-in missing from release: ${src_scope}"
  [[ -f "$src_live" ]] || die "live example missing from release: ${src_live}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: backup, install ${UNIT}, ${TIMER}, and ${WRITE_SCOPE_CONF} from ${RELEASE_DIR}"
    echo "dry-run: remove drop-ins that set ASCEND_DRIVER_MAILBOX or ASCEND_DRIVER_MAILBOXES"
    echo "dry-run: remove ASCEND_DRIVER_LIVE drop-ins before the verification dry run"
    echo "dry-run: ${SYSTEMCTL} daemon-reload"
    echo "dry-run: ${ANALYZE} verify ${ETC}/${UNIT}"
    echo "dry-run: ${SYSTEMCTL} start ${UNIT}"
    echo "dry-run: create ${STATE_DIR} mode 0755 owner ${UNIT_USER}"
    echo "dry-run: migrate note ledger into ${STATE_DIR}/discussion-note-ledger.json mode 0600"
    echo "dry-run: print ${STATE_DIR}/last-run.json"
    if [[ "$DO_LIVE" -eq 1 ]]; then
      echo "dry-run: install ${LIVE_CONF} from ${LIVE_EXAMPLE} after the dry run"
    else
      echo "dry-run: live drop-in not installed"
    fi
    if [[ "$ENABLE_TIMER" -eq 1 ]]; then
      echo "dry-run: ${SYSTEMCTL} enable --now ${TIMER}"
    else
      echo "dry-run: ${SYSTEMCTL} disable --now ${TIMER}"
    fi
    backup_tree >/dev/null
    return 0
  fi

  local backup
  backup="$(backup_tree | tail -n 1)"
  [[ "$backup" == "$BACKUP_ROOT"/* ]] || die "refusing unexpected backup path: ${backup}"
  echo "BACKUP=${backup}" >&2

  stop_timer
  ensure_dir "$ETC" 0755
  ensure_dir "$DROPIN_DIR" 0755
  ensure_dir "$STATE_DIR" 0755
  own_state_dir "$STATE_DIR"
  migrate_note_ledger

  install_file "$src_unit" "${ETC}/${UNIT}"
  install_file "$src_timer" "${ETC}/${TIMER}"
  install_file "$src_scope" "${DROPIN_DIR}/${WRITE_SCOPE_CONF}"

  remove_matching_dropins mailbox
  # The verification start must be a dry run even when --live is requested
  # and even when a previous drop-in turned live mode on.
  remove_matching_dropins live

  "${SYSTEMCTL}" daemon-reload
  "${ANALYZE}" verify "${ETC}/${UNIT}"
  "${SYSTEMCTL}" start "$UNIT" || die "verification dry run failed (see the unit status)"

  if [[ -f "${STATE_DIR}/last-run.json" ]]; then
    echo "summary:"
    cat "${STATE_DIR}/last-run.json"
  else
    echo "summary file not written at ${STATE_DIR}/last-run.json"
  fi

  if [[ "$DO_LIVE" -eq 1 ]]; then
    install_file "$src_live" "${DROPIN_DIR}/${LIVE_CONF}"
    "${SYSTEMCTL}" daemon-reload
    echo "live drop-in installed: ${DROPIN_DIR}/${LIVE_CONF}"
  else
    echo "live drop-in not installed (pass --live after Carlo's go)"
  fi

  if [[ "$ENABLE_TIMER" -eq 1 ]]; then
    "${SYSTEMCTL}" enable --now "$TIMER"
    echo "timer enabled: ${TIMER}"
  else
    "${SYSTEMCTL}" disable --now "$TIMER" || true
    echo "timer left stopped: ${TIMER}"
  fi
  echo "BACKUP=${backup}"
}

require_real_root

if [[ -n "$ROLLBACK" && -n "$RELEASE_DIR" ]]; then
  die "pass either --rollback or --release-dir, not both"
fi

if [[ -n "$ROLLBACK" ]]; then
  restore_from_backup "$ROLLBACK"
else
  install_release
fi
