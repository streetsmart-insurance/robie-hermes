#!/usr/bin/env bash
# Install the unmatched Ascend notice digest unit and weekday timer.
# Does not deploy a release and does not flip /opt/streetsmart-hermes/current.
# Does not set ASCEND_API_SOURCE_LIVE. Does not email unless --live is passed,
# and even then the verification start runs before the live drop-in exists.
#
# Preview one dry run. Does not enable the weekday timer, so it does not
# page PolicyApi every morning:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-unmatched-digest.sh \
#     --release-dir /opt/streetsmart-hermes/current \
#     --dry-run-once
#
# Separate step: enable the weekday timer. That is the daily PolicyApi
# paging. Mail stays a journal print until --live:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-unmatched-digest.sh \
#     --release-dir /opt/streetsmart-hermes/current \
#     --enable-timer
#
# After Carlo's explicit go to email hello@:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-unmatched-digest.sh \
#     --release-dir /opt/streetsmart-hermes/current \
#     --live
#
# Rollback disables the timer before the timer file is removed:
#   sudo /opt/streetsmart-hermes/current/scripts/install-ascend-unmatched-digest.sh \
#     --rollback /root/robie-ascend-unmatched-digest-YYYYMMDDTHHMMSSZ
#
# --enable-timer and --live write unmatched-digest-installed under
# data/ascend-api. Health watches the digest only when that marker exists
# or the timer is enabled. --dry-run-once does not write the marker.
# --rollback removes it.
#
# Tests pass --prefix and stub --systemctl / --systemd-analyze. --prefix is
# not for Production.

set -euo pipefail

UNIT="robie-ascend-unmatched-digest.service"
TIMER="robie-ascend-unmatched-digest.timer"
DROPIN_NAME="${UNIT}.d"
LIVE_EXAMPLE="30-live.conf.example"
LIVE_CONF="30-live.conf"

RELEASE_DIR=""
PREFIX=""
BACKUP_ROOT=""
ROLLBACK=""
DO_LIVE=0
DRY_ONCE=0
ENABLE_TIMER=0
SYSTEMCTL="${ASCEND_DIGEST_INSTALL_SYSTEMCTL:-systemctl}"
ANALYZE="${ASCEND_DIGEST_INSTALL_ANALYZE:-systemd-analyze}"

usage() {
  sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "install-ascend-unmatched-digest: $*" >&2
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
    --dry-run-once)
      DRY_ONCE=1
      shift
      ;;
    --enable-timer)
      ENABLE_TIMER=1
      shift
      ;;
    --live)
      DO_LIVE=1
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
else
  ETC="/etc/systemd/system"
  if [[ -z "$BACKUP_ROOT" ]]; then
    BACKUP_ROOT="/root"
  fi
fi

DROPIN_DIR="${ETC}/${DROPIN_NAME}"
MARKER_REL="opt/streetsmart-hermes/robie-job-engine/data/ascend-api/unmatched-digest-installed"
if [[ -n "$PREFIX" ]]; then
  MARKER="${PREFIX}/${MARKER_REL}"
else
  MARKER="/${MARKER_REL}"
fi

require_real_root() {
  if [[ -n "$PREFIX" ]]; then
    return 0
  fi
  [[ "${EUID}" -eq 0 ]] || die "installer requires sudo (or pass --prefix)"
}

install_file() {
  local src="$1"
  local dest="$2"
  if [[ "${EUID}" -eq 0 ]]; then
    install -o root -g root -m 0644 "$src" "$dest"
  else
    install -m 0644 "$src" "$dest"
  fi
}

ensure_dir() {
  local dir="$1"
  mkdir -p "$dir"
  chmod 0755 "$dir"
}

backup_tree() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
  dest="${BACKUP_ROOT}/robie-ascend-unmatched-digest-${stamp}"
  mkdir -p "$dest"
  {
    echo "timer_enabled=$(${SYSTEMCTL} is-enabled "$TIMER" 2>/dev/null || true)"
    echo "timer_active=$(${SYSTEMCTL} is-active "$TIMER" 2>/dev/null || true)"
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

stop_timer() {
  "${SYSTEMCTL}" stop "$TIMER" || true
}

restore_from_backup() {
  local backup="$1"
  [[ -d "$backup" ]] || die "backup dir not found: ${backup}"
  [[ -f "${backup}/manifest.txt" ]] || die "backup is missing manifest.txt: ${backup}"
  echo "restoring ${UNIT} from ${backup}"
  # Disable while the timer unit file is still on disk. Removing it first
  # makes systemd forget the unit and leave it active.
  echo "STEP disable --now"
  "${SYSTEMCTL}" disable --now "$TIMER" || true
  echo "STEP replace unit files"
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
  remove_installed_marker
  echo "ROLLBACK=${backup}"
  echo "timer left stopped"
}

write_installed_marker() {
  local dir
  dir="$(dirname "$MARKER")"
  mkdir -p "$dir"
  if [[ "$(basename "$dir")" == "ascend-api" ]]; then
    chmod 0755 "$dir"
  fi
  printf 'enabled %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER"
  chmod 0644 "$MARKER"
  echo "MARKER_WRITTEN"
}

remove_installed_marker() {
  rm -f "$MARKER"
  echo "MARKER_REMOVED"
}

install_release() {
  [[ -n "$RELEASE_DIR" ]] || die "--release-dir is required"
  [[ -d "$RELEASE_DIR" ]] || die "release dir not found: ${RELEASE_DIR}"
  local src_unit="${RELEASE_DIR}/deploy/systemd/${UNIT}"
  local src_timer="${RELEASE_DIR}/deploy/systemd/${TIMER}"
  local src_live="${RELEASE_DIR}/deploy/systemd/${DROPIN_NAME}/${LIVE_EXAMPLE}"
  [[ -f "$src_unit" ]] || die "unit missing from release: ${src_unit}"
  [[ -f "$src_timer" ]] || die "timer missing from release: ${src_timer}"
  [[ -f "$src_live" ]] || die "live example missing from release: ${src_live}"
  if grep -E -q '^[[:space:]]*Environment=.*ASCEND_UNMATCHED_DIGEST_LIVE=' "$src_unit"; then
    die "unit must not set ASCEND_UNMATCHED_DIGEST_LIVE; that belongs in the drop-in"
  fi

  local backup
  backup="$(backup_tree | tail -n 1)"
  [[ "$backup" == "$BACKUP_ROOT"/* ]] || die "refusing unexpected backup path: ${backup}"
  echo "BACKUP=${backup}" >&2

  stop_timer
  ensure_dir "$ETC"
  ensure_dir "$DROPIN_DIR"
  install_file "$src_unit" "${ETC}/${UNIT}"
  install_file "$src_timer" "${ETC}/${TIMER}"
  rm -f "${DROPIN_DIR}/${LIVE_CONF}"

  "${SYSTEMCTL}" daemon-reload
  "${ANALYZE}" verify "${ETC}/${UNIT}"

  # The verification start is always a dry run. --live installs the drop-in
  # only after this start returns.
  if [[ "$DRY_ONCE" -eq 1 || "$DO_LIVE" -eq 1 ]]; then
    "${SYSTEMCTL}" start "$UNIT" || die "verification dry run failed (see the unit status)"
  fi

  if [[ "$DO_LIVE" -eq 1 ]]; then
    install_file "$src_live" "${DROPIN_DIR}/${LIVE_CONF}"
    if ! grep -q 'ASCEND_UNMATCHED_DIGEST_LIVE=1' "${DROPIN_DIR}/${LIVE_CONF}"; then
      die "live drop-in does not set ASCEND_UNMATCHED_DIGEST_LIVE=1"
    fi
    "${SYSTEMCTL}" daemon-reload
  fi

  # --dry-run-once does not enable the weekday timer. That timer is the
  # daily PolicyApi page. --enable-timer is the separate step. --live
  # enables it after the dry verification start.
  if [[ "$DO_LIVE" -eq 1 || "$ENABLE_TIMER" -eq 1 ]]; then
    "${SYSTEMCTL}" enable --now "$TIMER"
    write_installed_marker
  else
    echo "TIMER_NOT_ENABLED"
  fi
  echo "INSTALLED=${UNIT}"
  if [[ "$DO_LIVE" -eq 1 ]]; then
    echo "LIVE=1"
  else
    echo "LIVE=0"
  fi
}

require_real_root

if [[ -n "$ROLLBACK" ]]; then
  if [[ "$DO_LIVE" -eq 1 || "$DRY_ONCE" -eq 1 || "$ENABLE_TIMER" -eq 1 ]]; then
    die "--rollback cannot be combined with --live, --dry-run-once, or --enable-timer"
  fi
  restore_from_backup "$ROLLBACK"
  exit 0
fi

if [[ "$DO_LIVE" -eq 0 && "$DRY_ONCE" -eq 0 && "$ENABLE_TIMER" -eq 0 ]]; then
  die "pass --dry-run-once, --enable-timer, --live, or --rollback"
fi

install_release
