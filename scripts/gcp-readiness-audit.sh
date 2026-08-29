#!/usr/bin/env bash
set -uo pipefail

: "${AUDIT_REPORT:?AUDIT_REPORT is required}"
: "${CONTROL_PROJECT:=streetsmart-robie-test}"
: "${RUNTIME_PROJECT:=streetsmart-hermes-poc}"
: "${GCP_ZONE:=us-east1-b}"

umask 077

verified=0
unverified=0
required_unverified=0

record_verified() {
  verified=$((verified + 1))
  printf '| `%s` | VERIFIED | `%s` |\n' "$1" "$2" >>"$AUDIT_REPORT"
}

record_unverified() {
  unverified=$((unverified + 1))
  printf '| `%s` | UNVERIFIED | `%s` |\n' "$1" "$2" >>"$AUDIT_REPORT"
}

sanitize_error() {
  tr '\n' ' ' <"$1" | sed -E 's/[[:space:]]+/ /g; s/`/'"'"'/g' | cut -c1-240
}

probe_instance() {
  local project="$1"
  local instance="$2"
  local required="$3"
  local output error_file
  error_file="$(mktemp)"
  if output="$(gcloud compute instances describe "$instance" \
      --project="$project" \
      --zone="$GCP_ZONE" \
      --format='value(name,status,zone.basename(),machineType.basename(),lastStartTimestamp,lastStopTimestamp)' \
      2>"$error_file")"; then
    record_verified "instance:${project}/${instance}" "$output"
  else
    record_unverified "instance:${project}/${instance}" "$(sanitize_error "$error_file")"
    if [[ "$required" == 'required' ]]; then
      required_unverified=$((required_unverified + 1))
    fi
  fi
  rm -f "$error_file"
}

active_account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n 1)"
if [[ -z "$active_account" ]]; then
  echo "No active short-lived GCP identity" >&2
  exit 1
fi

{
  echo '# GCP readiness audit'
  echo
  printf -- '- Generated: `%s`\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '- GitHub repository: `%s`\n' "${GITHUB_REPOSITORY:-unknown}"
  printf -- '- GitHub commit: `%s`\n' "${GITHUB_SHA:-unknown}"
  printf -- '- Short-lived identity: `%s`\n' "$active_account"
  echo '- Scope: selected resource-plane reads only; no SSH, secrets, restart, or deploy.'
  echo
  echo '| Check | Verdict | Selected result or bounded error |'
  echo '| --- | --- | --- |'
} >"$AUDIT_REPORT"

probe_instance "$CONTROL_PROJECT" 'antigravity-test-01' 'informational'
probe_instance "$RUNTIME_PROJECT" 'hermes-test-01' 'required'
probe_instance "$RUNTIME_PROJECT" 'hermes-poc-01' 'required'

{
  echo
  printf -- '- Verified checks: `%s`\n' "$verified"
  printf -- '- Unverified checks: `%s`\n' "$unverified"
  printf -- '- Required runtime checks unverified: `%s`\n' "$required_unverified"
  echo '- Runtime application versions: `UNVERIFIED` (resource-plane status is not loaded-code proof).'
} >>"$AUDIT_REPORT"

cat "$AUDIT_REPORT"

# Required permission gaps are reported as UNVERIFIED and fail the readiness
# check. Authentication success alone must never produce a green readiness gate.
if (( required_unverified > 0 )); then
  exit 2
fi
