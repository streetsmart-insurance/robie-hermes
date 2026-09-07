#!/usr/bin/env bash
# Install the reviewed accountability units on the designated Production host.
set -euo pipefail

EXPECTED_HOST="hermes-poc-01"
OPT_ROOT="/opt/streetsmart-hermes"
RELEASE_ROOT="$(readlink -f "${OPT_ROOT}/releases/current")"
MANIFEST="${OPT_ROOT}/accountability/connection-manifest.json"
ACCOUNTABILITY_ENV="/etc/streetsmart-hermes/robie-accountability.env"
EZLYNX_ENV="/etc/streetsmart-hermes/robie-ezlynx.env"

[[ "${EUID}" -eq 0 ]] || { echo "Production activation requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || {
  echo "refusing non-Production host: $(hostname -s)" >&2
  exit 2
}
[[ -d "${RELEASE_ROOT}" && -f "${MANIFEST}" ]] || {
  echo "release or accountability manifest is unavailable" >&2
  exit 2
}
[[ -f "${ACCOUNTABILITY_ENV}" && -f "${EZLYNX_ENV}" ]] || {
  echo "accountability or dedicated Robie EZLynx environment file is unavailable" >&2
  exit 2
}

python3 - "${MANIFEST}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
safety = dict(data.get("safety") or {})
rules = dict(data.get("rules") or {})
if str(safety.get("environment") or "").casefold() != "production":
    raise SystemExit("manifest must declare safety.environment=Production")
if rules.get("daily_reporting_period") != "previous_business_day":
    raise SystemExit("manifest must use daily_reporting_period=previous_business_day")
if not rules.get("require_complete_evidence", True):
    raise SystemExit("Production must fail closed on incomplete evidence")
PY

readiness="$(
  PYTHONPATH="${RELEASE_ROOT}" \
    /opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python \
    -m robie_job_engine.accountability_connections --manifest "${MANIFEST}"
)"
python3 - "${readiness}" "${MANIFEST}" <<'PY'
import json
import pathlib
import sys

state = json.loads(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
required = list((manifest.get("rules") or {}).get(
    "production_required_connections",
    ["ringcentral", "ezlynx", "gmail", "magellan", "google_sheets", "delivery"],
))
connections = dict(state.get("connections") or {})
missing = [name for name in required if not (connections.get(name) or {}).get("ready")]
if missing:
    raise SystemExit(
        "required Production accountability connections are not ready: "
        + ", ".join(missing)
    )
print(json.dumps({"ready": True, "required_connections": required}, sort_keys=True))
PY

install -o root -g root -m 0644 \
  "${RELEASE_ROOT}/deploy/systemd/streetsmart-accountability.service" \
  /etc/systemd/system/streetsmart-accountability.service
install -o root -g root -m 0644 \
  "${RELEASE_ROOT}/deploy/systemd/streetsmart-accountability.timer" \
  /etc/systemd/system/streetsmart-accountability.timer
systemctl daemon-reload
systemctl enable --now streetsmart-accountability.timer
systemctl is-enabled --quiet streetsmart-accountability.timer
systemctl is-active --quiet streetsmart-accountability.timer
systemctl show streetsmart-accountability.timer \
  -p NextElapseUSecRealtime -p LastTriggerUSec -p Unit --no-pager
systemctl show streetsmart-accountability.service \
  -p ExecStart -p User -p WorkingDirectory --no-pager
