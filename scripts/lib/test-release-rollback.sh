#!/usr/bin/env bash
# Shared Test-release pointer rollback primitives. This file is sourced by the
# Test installer and by an isolated behavioral regression test.

atomic_pointer() {
  python3 - "$1" "$2" <<'PY'
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1]).resolve(strict=True)
link = pathlib.Path(sys.argv[2])
tmp = link.with_name(link.name + ".rollback-new")
try:
    tmp.unlink()
except FileNotFoundError:
    pass
tmp.symlink_to(target)
os.replace(tmp, link)
PY
}

restore_file_snapshot() {
  local state="$1"
  local snapshot="$2"
  local destination="$3"

  case "${state}" in
    present)
      test -f "${snapshot}"
      install -D -m 0644 "${snapshot}" "${destination}"
      ;;
    absent)
      rm -f -- "${destination}"
      ;;
    *)
      echo "unknown file snapshot state: ${state}" >&2
      return 2
      ;;
  esac
}

rollback_test_release() {
  local old_current="$1"
  local old_releases_current="$2"
  local old_policy_skill_target="$3"
  local current_link="$4"
  local releases_current_link="$5"
  local policy_skill_link="$6"
  local gateway_unit="$7"
  local systemctl_bin="${ROBIE_SYSTEMCTL:-systemctl}"

  atomic_pointer "${old_current}" "${current_link}"
  atomic_pointer "${old_releases_current}" "${releases_current_link}"
  if [[ -n "${old_policy_skill_target}" ]]; then
    atomic_pointer "${old_policy_skill_target}" "${policy_skill_link}"
  else
    rm -f "${policy_skill_link}"
  fi
  "${systemctl_bin}" restart "${gateway_unit}"
  "${systemctl_bin}" is-active --quiet "${gateway_unit}"
}
