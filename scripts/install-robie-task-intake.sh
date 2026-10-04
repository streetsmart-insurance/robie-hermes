#!/usr/bin/env bash
# Install the EZLynx Task Check-In intake and its health timer.
#
# Run on hermes-poc-01 after this commit is the current release.
# Does not print secrets and does not source env files.
#
#   --dry-run-once   install units, start intake once with
#                    ROBIE_TASK_INTAKE_DRY_RUN=1, then remove that drop-in.
#                    The one-shot does not write EZLynx, does not create job
#                    rows, and does not enable timers or the live drop-in.
#   --enable-timer   install units and enable both timers. Removes a
#                    leftover 10-dry-run.conf so the timer cannot stay dry.
#   --live           install the Bland live drop-in and reload systemd.
#                    Removes a leftover 10-dry-run.conf.
#   --rollback       stop and disable the timers, remove the units, the
#                    live drop-in, and any leftover 10-dry-run.conf.
#                    Backups under the backup root are kept.
#
# Tests point ROBIE_UNIT_DEST, ROBIE_RELEASE_ROOT, ROBIE_SYSTEMCTL, and
# ROBIE_BACKUP_ROOT at temp paths. Root is required only for the real
# /etc/systemd/system destination.
set -euo pipefail

DEST="${ROBIE_UNIT_DEST:-/etc/systemd/system}"
RELEASE_ROOT="${ROBIE_RELEASE_ROOT:-/opt/streetsmart-hermes/releases/current}"
SYSTEMCTL="${ROBIE_SYSTEMCTL:-systemctl}"
BACKUP_ROOT="${ROBIE_BACKUP_ROOT:-/var/backups/robie-task-intake}"

UNITS=(
  robie-task-intake.service
  robie-task-intake.timer
  robie-task-intake-health.service
  robie-task-intake-health.timer
)
DROPIN_DIR="robie-task-intake.service.d"
DROPIN_NAME="20-bland-prod.conf"

usage() {
  echo "usage: $0 --dry-run-once | --enable-timer | --live | --rollback" >&2
  exit 2
}

mode=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run-once|--enable-timer|--live|--rollback)
      if [[ -n "${mode}" ]]; then
        echo "pass one mode at a time" >&2
        exit 2
      fi
      mode="$1"
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
  shift
done
[[ -n "${mode}" ]] || usage

if [[ "${DEST}" == "/etc/systemd/system" && "${EUID}" -ne 0 ]]; then
  echo "installer requires sudo" >&2
  exit 2
fi

backup_existing() {
  local stamp dest_dir unit copied
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest_dir="${BACKUP_ROOT}/${stamp}"
  mkdir -p "${dest_dir}"
  copied=0
  for unit in "${UNITS[@]}"; do
    if [[ -f "${DEST}/${unit}" ]]; then
      cp -a "${DEST}/${unit}" "${dest_dir}/${unit}"
      copied=1
    fi
  done
  if [[ -f "${DEST}/${DROPIN_DIR}/${DROPIN_NAME}" ]]; then
    mkdir -p "${dest_dir}/${DROPIN_DIR}"
    cp -a "${DEST}/${DROPIN_DIR}/${DROPIN_NAME}" "${dest_dir}/${DROPIN_DIR}/${DROPIN_NAME}"
    copied=1
  fi
  if [[ "${copied}" -eq 0 ]]; then
    echo "no existing units to back up; pointer ${dest_dir}"
  else
    echo "backed up existing units to ${dest_dir}"
  fi
}

install_units() {
  local unit source
  mkdir -p "${DEST}"
  for unit in "${UNITS[@]}"; do
    source="${RELEASE_ROOT}/deploy/systemd/${unit}"
    if [[ ! -f "${source}" ]]; then
      echo "unit missing from release: ${unit}" >&2
      exit 2
    fi
    install -m 0644 "${source}" "${DEST}/${unit}"
  done
}

remove_dry_run_dropin() {
  rm -f "${DEST}/${DROPIN_DIR}/10-dry-run.conf"
}

# EnvironmentFile= overrides Environment= no matter the order in the unit.
# Confirm the effective environment, not the unit text, and read every
# EnvironmentFile. A file that assigns either key wins over Environment=.
verify_effective_write_scope() {
  local shown
  shown="$("${SYSTEMCTL}" show robie-task-intake.service -p Environment --no-pager 2>/dev/null || true)"
  if [[ "${shown}" != *"ROBIE_EZLYNX_WRITE_SCOPE=all"* ]]; then
    echo "systemctl show -p Environment is missing ROBIE_EZLYNX_WRITE_SCOPE=all" >&2
    exit 2
  fi
}

env_file_assigns_scope() {
  local path line
  path="$1"
  [[ -f "${path}" ]] || return 1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "${line}" ]] && continue
    if [[ "${line}" =~ ^(export[[:space:]]+)?ROBIE_EZLYNX_WRITE_SCOPE= ]] \
      || [[ "${line}" =~ ^(export[[:space:]]+)?ROBIE_PLAYGROUND= ]]; then
      return 0
    fi
  done < "${path}"
  return 1
}

verify_environment_files() {
  local shown rest token
  shown="$("${SYSTEMCTL}" show robie-task-intake.service -p EnvironmentFiles --no-pager 2>/dev/null || true)"
  rest="${shown#*EnvironmentFiles=}"
  for token in ${rest}; do
    [[ "${token}" == /* ]] || continue
    if env_file_assigns_scope "${token}"; then
      echo "an EnvironmentFile sets ROBIE_EZLYNX_WRITE_SCOPE or ROBIE_PLAYGROUND" >&2
      exit 2
    fi
  done
}

verify_effective_env() {
  verify_effective_write_scope
  verify_environment_files
}

case "${mode}" in
  --rollback)
    "${SYSTEMCTL}" disable --now robie-task-intake.timer robie-task-intake-health.timer || true
    "${SYSTEMCTL}" stop robie-task-intake.service robie-task-intake-health.service || true
    rm -f "${DEST}/robie-task-intake.service" \
          "${DEST}/robie-task-intake.timer" \
          "${DEST}/robie-task-intake-health.service" \
          "${DEST}/robie-task-intake-health.timer" \
          "${DEST}/${DROPIN_DIR}/${DROPIN_NAME}" \
          "${DEST}/${DROPIN_DIR}/10-dry-run.conf"
    rmdir "${DEST}/${DROPIN_DIR}" 2>/dev/null || true
    "${SYSTEMCTL}" daemon-reload || true
    echo "ROLLBACK_DONE"
    ;;
  --dry-run-once)
    if [[ -f "${DEST}/${DROPIN_DIR}/${DROPIN_NAME}" ]]; then
      echo "refusing dry-run-once while the live drop-in is installed" >&2
      exit 2
    fi
    backup_existing
    install_units
    mkdir -p "${DEST}/${DROPIN_DIR}"
    dry_dropin="${DEST}/${DROPIN_DIR}/10-dry-run.conf"
    cleanup_dry() {
      rm -f "${dry_dropin}"
      rmdir "${DEST}/${DROPIN_DIR}" 2>/dev/null || true
      "${SYSTEMCTL}" daemon-reload || true
    }
    trap cleanup_dry EXIT
    printf '%s\n' '[Service]' 'Environment=ROBIE_TASK_INTAKE_DRY_RUN=1' > "${dry_dropin}"
    "${SYSTEMCTL}" daemon-reload
    verify_effective_env
    "${SYSTEMCTL}" start robie-task-intake.service
    trap - EXIT
    cleanup_dry
    echo "DRY_RUN_ONCE_DONE"
    ;;
  --enable-timer)
    backup_existing
    install_units
    remove_dry_run_dropin
    rmdir "${DEST}/${DROPIN_DIR}" 2>/dev/null || true
    "${SYSTEMCTL}" daemon-reload
    verify_effective_env
    "${SYSTEMCTL}" enable --now robie-task-intake.timer robie-task-intake-health.timer
    echo "TIMERS_ENABLED"
    ;;
  --live)
    backup_existing
    install_units
    remove_dry_run_dropin
    # Check the effective environment before the live drop-in exists.
    # A failed check must not leave live calls switched on.
    "${SYSTEMCTL}" daemon-reload
    verify_effective_env
    mkdir -p "${DEST}/${DROPIN_DIR}"
    install -m 0644 \
      "${RELEASE_ROOT}/deploy/systemd/${DROPIN_DIR}/${DROPIN_NAME}.example" \
      "${DEST}/${DROPIN_DIR}/${DROPIN_NAME}"
    "${SYSTEMCTL}" daemon-reload
    echo "LIVE_DROPIN_INSTALLED"
    ;;
esac
