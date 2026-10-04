#!/usr/bin/env python3
"""
ROBIE hourly health check — eyes and ears on the box.

Runs via cron (hourly). Checks the things that have actually bitten us:
  1. Worker process alive + last poll time
  2. Loaded CODE_VERSION vs latest merged commit on main
  3. Required env vars present (names only, never values)
  4. HITL dry-run: Gemini reachable? Email path constructible? Chat webhook present?
  5. Stuck job leases
  6. Disk usage (/tmp screenshots pile up)
  7. Credential probes (auth-only): phone-watchdog Gmail SA key, RingCentral JWT,
     EZLynx OAuth token — catches dead keys like the 2026-09-27 invalid_grant
     that silenced the Sonant sweep 09:19–11:22 EDT with no alert.
  8. Sweep freshness: is each scheduled job producing successful runs on time?
  9. Service error scan: failure signatures (invalid_grant, tracebacks) in the
     trailing journal window per service.
  10. Systems watchdog phase 2 (2026-09-28):
      - Both phone Gmail keys probed separately (primary + backup)
      - EZLynx login secret version states (ENABLED? states only, no payloads)
      - Applicant ingest freshness (warn 30h, fail 36h — the fail-closed cliff)
      - EOD local output proof + Drive delivery UNVERIFIED flag
      - Task-verifier health (stuck PENDING/UNVERIFIED tasks, journal errors)
      - 4359 Tuesday email proof (evidence-latest.json from most recent Tue)
      - Chat intake liveness (reuses production_preflight.check_chat_intake)
      - Preflight alert delivery (journal JSON alert_delivery_failed)
  11. Ascend notice driver stall: last 4 live runs saw actionable notices
      and filed nothing. Reads the counts file the driver writes. Does
      not read the journal (the health-check user cannot).

Output:
  - JSON status file (always written, even when healthy)
  - Google Chat ping ONLY on failure (quiet when healthy)
  - --daily-digest: morning all-green/failure digest (phase 3; daily timer)
  - Exit 0 = healthy, 1 = issues found, 2 = check itself errored

All probes are AUTH-ONLY / READ-ONLY: no operations, no writes, no sends.
A check must never crash the run; failures are reported, not raised.
Secret values are never logged — names and statuses only.

Usage (on hermes-poc-01):
  python3 robie_health_check.py [--status-dir /tmp/robie-health] [--no-chat]

Test (hermes-test-01) uses the same script with a Test profile. Prod-only
phone, EOD, 4359, and Chat-intake probes are skipped. Test units and paths
are checked instead. Unset ROBIE_ENV stays the Production list.

  ROBIE_ENV=TEST python3 robie_health_check.py --no-chat
  python3 robie_health_check.py --profile TEST --no-chat

Cron (hourly, quiet on success):
  0 * * * * /usr/bin/python3 /opt/streetsmart-hermes/scripts/robie_health_check.py >> /var/log/robie-health.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "ROBIE_VERIFICATION_MAIL_SENDER",
    "ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT",
    "ROBIE_GEMINI_PROJECT",
    "ROBIE_GOOGLE_CHAT_WEBHOOK_URL",
]

# How old (seconds) a job lease can be before we flag it as stuck.
STUCK_LEASE_SECONDS = 3600

# /tmp usage percent that triggers a warning.
TMP_WARN_PCT = 85

# Used only when no release tree and no environment override are visible.
# The Production cron copy at /opt/streetsmart-hermes/scripts/ still resolves
# to this path through the releases/current directory beside that prefix.
_PROD_RELEASE_FALLBACK = "/opt/streetsmart-hermes/releases/current"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contains_job_engine(path: str) -> bool:
    return os.path.isdir(os.path.join(path, "robie_job_engine"))


def release_import_root(script_file: str | None = None) -> str:
    """Directory to prepend before importing ``robie_job_engine``.

    The health check ships inside the release it should import:

    * ``ROBIE_CANONICAL_JOB_ENGINE_ROOT`` wins when the systemd unit sets it.
    * Otherwise the parent of this file's ``scripts/`` directory, when that
      directory contains the package (the release tree, Test or Production).
    * Otherwise ``<prefix>/releases/current`` when this file is the cron copy
      at ``<prefix>/scripts/``. On Production that prefix is
      ``/opt/streetsmart-hermes``, so the import root stays the historical path.
    * Otherwise the first ``PYTHONPATH`` entry that contains the package.
      The Test gateway drop-in lists ``.gateway-runtime`` ahead of the release;
      the runtime directory is skipped.
    * Otherwise the historical Production pointer, so a cron run that cannot
      see a release tree still imports what it imports today.
    """
    override = os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT", "").strip()
    if override:
        return override

    script = os.path.abspath(script_file or __file__)
    scripts_dir = os.path.dirname(script)
    shipped = os.path.dirname(scripts_dir)
    if _contains_job_engine(shipped):
        return shipped

    cron_release = os.path.join(shipped, "releases", "current")
    if _contains_job_engine(cron_release):
        return cron_release

    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        candidate = entry if os.path.isabs(entry) else os.path.abspath(entry)
        if _contains_job_engine(candidate):
            return candidate

    return _PROD_RELEASE_FALLBACK


def _prepend_release_import() -> str:
    root = release_import_root()
    sys.path.insert(0, root)
    return root


# Test profile roots. Production checks keep the historical paths above.
TEST_ROOT = "/opt/streetsmart-hermes-test"
TEST_RELEASE_ROOT = TEST_ROOT + "/releases/current"
TEST_JOBS_DB = TEST_ROOT + "/robie-job-engine/data/jobs.db"
TEST_GATEWAY_UNIT = "robie-gateway.service"
TEST_BROWSER_UNIT = "robie-ezlynx-browser-test.service"
TEST_KEEPALIVE_TIMER = "robie-ezlynx-keepalive-test.timer"
TEST_KEEPALIVE_SERVICE = "robie-ezlynx-keepalive-test.service"
TEST_ERROR_SCAN_SERVICES = (
    "robie-gateway.service",
    "robie-ezlynx-browser-test.service",
    "robie-ezlynx-keepalive-test.service",
)


# Set for the duration of execute() so probes see --profile even when
# ROBIE_ENV is unset. None means "follow the environment".
_REQUESTED_PROFILE: str | None = None


def resolve_health_profile(explicit: str | None = None) -> str:
    """PRODUCTION unless --profile or ROBIE_ENV selects TEST.

    An unset environment stays Production so the existing cron is unchanged.
    """
    if explicit:
        token = explicit
    elif _REQUESTED_PROFILE:
        token = _REQUESTED_PROFILE
    else:
        token = os.environ.get("ROBIE_ENV", "")
    if str(token or "").strip().upper() == "TEST":
        return "TEST"
    return "PRODUCTION"


def _code_version_import_root() -> str:
    """Production uses the script's release. Test prefers the Test pointer."""
    if (
        resolve_health_profile() == "TEST"
        and _contains_job_engine(TEST_RELEASE_ROOT)
    ):
        sys.path.insert(0, TEST_RELEASE_ROOT)
        return TEST_RELEASE_ROOT
    return _prepend_release_import()


# ---------------------------------------------------------------------------
# Individual checks — each returns (ok: bool, detail: str, extra: dict)
# ---------------------------------------------------------------------------

def check_worker_alive() -> tuple[bool, str, dict]:
    """Is the email worker process running? When did it last poll?"""
    extra: dict = {}
    try:
        # Look for the worker process. Adjust the match string to the
        # actual worker entrypoint on the box.
        out = subprocess.run(
            ["pgrep", "-af", "robie.*worker|email.*agent|hermes.*gateway"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in out.stdout.splitlines() if "pgrep" not in l]
        extra["matching_processes"] = len(lines)
        if not lines:
            return False, "no worker process found", extra
        # Last-poll marker: the worker touches a file each poll if configured.
        # Fall back to process start time.
        extra["sample"] = lines[0][:120]
        return True, f"{len(lines)} worker process(es) running", extra
    except Exception as exc:
        return False, f"process check failed: {type(exc).__name__}", extra


def check_code_version() -> tuple[bool, str, dict]:
    """What CODE_VERSION is importable? Does it match main's HEAD?"""
    extra: dict = {}
    try:
        # NOTE 2026-09-27: was "releases/current/robie-main2" which does not
        # exist — the import always failed. The package lives directly under
        # the release root this script shipped in.
        link = _code_version_import_root()
        from robie_job_engine.ezlynx_policy_setup import CODE_VERSION
        extra["loaded_version"] = CODE_VERSION
    except Exception as exc:
        return False, f"cannot import CODE_VERSION: {type(exc).__name__}: {exc}", extra

    # Compare against the deployed release symlink target (no network needed).
    try:
        target = os.readlink(link) if os.path.islink(link) else ""
        extra["release_target"] = target
        # The release dir is usually named with the commit, e.g. .../68954cc3...
        if extra["loaded_version"] and extra["loaded_version"][:8] not in target:
            return False, (
                f"loaded {extra['loaded_version'][:12]} but release dir is {target} "
                "(worker may be running stale code)"
            ), extra
    except Exception as exc:
        extra["release_check_error"] = f"{type(exc).__name__}"
    return True, f"CODE_VERSION={extra['loaded_version'][:12]}", extra


def check_env_vars() -> tuple[bool, str, dict]:
    """Are required env vars set? Names only — never log values."""
    extra: dict = {}
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]
    extra["checked"] = REQUIRED_ENV_VARS
    extra["missing"] = missing
    extra["present_count"] = len(REQUIRED_ENV_VARS) - len(missing)
    if missing:
        return False, f"missing env vars: {', '.join(missing)}", extra
    return True, f"all {len(REQUIRED_ENV_VARS)} required env vars present", extra


def check_hitl_dry_run() -> tuple[bool, str, dict]:
    """Can the HITL path at least construct its pieces (no sends)?"""
    extra: dict = {}
    problems: list[str] = []

    # Gemini client constructible + configured?
    try:
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current/robie-main2")
        from robie_job_engine.gemini_field_helper import VertexGeminiFieldClient
        client = VertexGeminiFieldClient()
        extra["gemini_configured"] = client.configured()
        extra["gemini_has_generate_content"] = hasattr(client, "generate_content")
        if not client.configured():
            problems.append("gemini client not configured (no project)")
        if not hasattr(client, "generate_content"):
            problems.append("gemini client missing generate_content (HITL will fail)")
    except Exception as exc:
        problems.append(f"gemini import failed: {type(exc).__name__}")
        extra["gemini_configured"] = False

    # Email sender importable?
    try:
        from robie_job_engine import verification_mailer
        extra["email_module"] = True
        # Do NOT send — just verify the config path raises a clear error
        # when misconfigured rather than at 2 AM during a real HITL.
        sender = verification_mailer._sender()
        sa = verification_mailer._delegated_service_account()
        extra["email_sender_set"] = bool(sender)
        extra["email_sa_set"] = bool(sa)
        if not sa:
            problems.append("delegated Gmail service account not configured")
    except Exception as exc:
        problems.append(f"email module import failed: {type(exc).__name__}")
        extra["email_module"] = False

    # Chat webhook present? (The actual send is a simple POST; presence is the check.)
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    extra["chat_webhook_present"] = bool(webhook)
    if not webhook:
        problems.append("ROBIE_GOOGLE_CHAT_WEBHOOK_URL not set")

    if problems:
        return False, "; ".join(problems), extra
    return True, "gemini + email + chat all constructible", extra


def check_stuck_leases() -> tuple[bool, str, dict]:
    """Any job leases older than STUCK_LEASE_SECONDS?"""
    extra: dict = {}
    # Lease state location is box-specific; try the common spots.
    candidates = [
        "/opt/streetsmart-hermes/job_state",
        "/tmp/robie-leases",
        os.path.expanduser("~/.robie/leases"),
    ]
    checked = []
    stuck = []
    now = time.time()
    for d in candidates:
        if not os.path.isdir(d):
            continue
        checked.append(d)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                continue
            if age > STUCK_LEASE_SECONDS:
                stuck.append({"lease": name, "age_seconds": int(age)})
    extra["dirs_checked"] = checked
    extra["stuck"] = stuck
    if stuck:
        return False, f"{len(stuck)} stuck lease(s)", extra
    suffix = f" ({len(checked)} dirs)" if checked else " (no lease dirs found)"
    return True, "no stuck leases" + suffix, extra


def check_disk() -> tuple[bool, str, dict]:
    """Is /tmp filling up (screenshots accumulate)?"""
    extra: dict = {}
    try:
        st = os.statvfs("/tmp")
        pct = 100 * (1 - st.f_bavail / st.f_blocks)
        extra["tmp_used_pct"] = round(pct, 1)
        # Size of the HITL screenshot dir, if present.
        shot_dir = "/tmp/robie-hitl-screenshots"
        if os.path.isdir(shot_dir):
            total = 0
            for root, _, files in os.walk(shot_dir):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            extra["hitl_screenshots_mb"] = round(total / 1e6, 1)
        if pct >= TMP_WARN_PCT:
            return False, f"/tmp {pct:.0f}% full", extra
        return True, f"/tmp {pct:.0f}% used", extra
    except Exception as exc:
        return False, f"disk check failed: {type(exc).__name__}", extra


# ---------------------------------------------------------------------------
# Server-wide watchdog checks (added 2026-09-27)
#
# Incident: the phone-watchdog's Gmail service account key was deleted on
# Google's side (invalid_grant). The Sonant sweep failed silently 09:19–11:22
# EDT and nothing alerted anyone — the checks above don't probe credential
# validity, sweep freshness, or per-service errors. These checks close that gap.
#
# All probes are AUTH-ONLY / READ-ONLY: no operations, no writes, no sends.
# Secret values are never logged — names and statuses only.
# ---------------------------------------------------------------------------

# Phone-watchdog service key resolution: systemd drop-in override wins over the
# base unit, which wins over the repo default. (On 2026-09-27 the live key is
# service_key_hermes_poc.json via the drop-in; service_key.json is dead.)
WATCHDOG_UNIT_PATHS = [
    "/etc/systemd/system/streetsmart-phone-watchdog.service.d/override.conf",
    "/etc/systemd/system/streetsmart-phone-watchdog.service",
    "/lib/systemd/system/streetsmart-phone-watchdog.service",
]
WATCHDOG_DEFAULT_KEY = "/opt/streetsmart-phone-watchdog/service_key.json"
WATCHDOG_MAILBOX = "carlo@streetsmart.insurance"

# Freshness: max seconds of sweep silence before we alert. The watchdog runs a
# continuous ~5-minute sweep loop, so 20 minutes = ~4 missed cycles.
SWEEP_STALE_SECONDS = 20 * 60

# Journal failure signatures that always warrant an alert.
ERROR_PATTERNS = [
    "invalid_grant",
    "Invalid signature for token",
    "Traceback (most recent call last)",
]

# Services whose journals we scan for ERROR_PATTERNS (trailing 1 hour).
ERROR_SCAN_SERVICES = [
    "streetsmart-phone-watchdog.service",
    "streetsmart-phone-eod.service",
    "robie-4359-policy-change.service",
]

# Timer jobs and their freshness limits. max_age_seconds is generous on purpose:
# it must not false-alarm across normal schedule gaps (weekends, weekly jobs).
# (timer_name, max_age_seconds, description)
TIMER_FRESHNESS = [
    ("streetsmart-phone-eod.timer", 100 * 3600, "EOD phone report (Mon–Fri 17:00)"),
    ("robie-4359-policy-change.timer", 10 * 24 * 3600, "4359 worker (Tue 08:00 weekly)"),
    ("streetsmart-phone-hourly-missed.timer", 70 * 3600, "missed-call digest (Mon–Fri 2-hourly)"),
]

GCP_PROJECT = os.environ.get("GCP_PROJECT", "streetsmart-hermes-poc")


# ---------------------------------------------------------------------------
# Systems watchdog phases 2–3 (added 2026-09-28)
#
# Phase 2 closes the monitoring gaps found in the 2026-09-28 inventory:
# both phone Gmail keys (not just the live one), applicant ingest freshness
# (36h cliff), EOD Drive delivery proof, task-verifier health, 4359 Tuesday
# email proof, Chat intake liveness, and Secret Manager version states.
#
# Phase 3 adds the daily "all green" digest (explicit healthy confirmation
# instead of silence-means-healthy) via --daily-digest, and the
# accountability-VM probe (scripts/accountability_vm_health_probe.py).
#
# All probes are AUTH-ONLY / READ-ONLY: no operations, no writes, no sends.
# Secret values are never logged — names and statuses only.
# ---------------------------------------------------------------------------

# Both phone-watchdog Gmail keys, probed separately. The primary died on
# 2026-09-27 (invalid_grant); the backup is the live one. If the primary is
# ever restored, this probe confirms it without masking the backup's state.
PHONE_GMAIL_KEYS = [
    ("/opt/streetsmart-phone-watchdog/service_key.json", "primary"),
    ("/opt/streetsmart-phone-watchdog/service_key_hermes_poc.json", "backup"),
]

# Applicant ingest freshness: the "All Applicants Phone Match Export" must be
# < 36h old or unknown callers fail closed (no callback tasks). Warn at 30h
# so there's a 6h window to fix it before the cliff.
APPLICANT_EXPORT_PATH = "/opt/streetsmart-phone-watchdog/data/applicant_phone_match_export.xls"
APPLICANT_WARN_SECONDS = 30 * 3600
APPLICANT_FAIL_SECONDS = 36 * 3600

# EOD outputs land here locally; Drive delivery is verified separately.
EOD_OUTPUT_DIR = "/opt/streetsmart-phone-watchdog/data/outputs"

# EOD Sheet delivery (post-2026-09-28: Sheets replaced Excel+Drive upload).
# The probe confirms the OUTCOME (most recent run's Sheet exists in Drive).
EOD_SHEET_NAME_FMT = "EOD Phone Report -- {date}"  # date = YYYY-MM-DD
EOD_SHEETS_FOLDER_ID_ENV = "EOD_SHEETS_FOLDER_ID"
EOD_SA_KEY_PATH = "/opt/streetsmart-phone-watchdog/service_key.json"

# Task verifier DB: set ROBIE_TASK_VERIFY_DB to override (matches the
# verifier service's own env). Stuck = PENDING/UNVERIFIED older than this.
TASK_VERIFY_DB_DEFAULT = "/home/carlo_streetsmart_insurance/.robie/task_verification/pending.db"
TASK_VERIFY_STUCK_SECONDS = 2 * 3600  # verifier runs every 15m; 2h stuck = broken

# 4359 Tuesday evidence.
EVIDENCE_4359_PATH = ("/opt/streetsmart-hermes/robie-job-engine/data/"
                      "overdue_policy_change_reports/evidence-latest.json")


def _resolve_watchdog_service_key() -> str:
    """Return the --service-key path the phone-watchdog service actually uses.

    Reads the systemd unit files in override order; the first --service-key
    found wins. Falls back to the repo default when nothing is configured.
    """
    for path in WATCHDOG_UNIT_PATHS:
        try:
            with open(path) as f:
                content = f.read()
        except OSError:
            continue
        m = re.search(r"--service-key\s+(\S+)", content)
        if m:
            return m.group(1)
    return WATCHDOG_DEFAULT_KEY


def _read_secret(secret_id: str) -> str:
    """Read one Secret Manager secret (latest version). Empty string on failure."""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT}/secrets/{secret_id}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8").strip()
    except Exception:
        return ""


def check_gmail_sa_key() -> tuple[bool, str, dict]:
    """Is the phone-watchdog's Gmail service account key alive?

    Resolves the LIVE key from systemd (not a hardcoded filename), then does
    an auth-only Gmail getProfile with domain-wide delegation — the exact call
    chain that died on 2026-09-27 with invalid_grant.
    """
    extra: dict = {}
    try:
        key_path = _resolve_watchdog_service_key()
        extra["key_path"] = key_path
        with open(key_path) as f:
            key_data = json.load(f)
        client_email = str(key_data.get("client_email", ""))
        extra["client_email"] = client_email
        if not key_data.get("private_key") or not client_email:
            return False, f"key file {key_path} is not a valid SA key", extra

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            subject=WATCHDOG_MAILBOX,
        )
        svc = build("gmail", "v1", credentials=creds)
        profile = svc.users().getProfile(userId="me").execute()
        extra["gmail_user"] = profile.get("emailAddress", "")
        return True, f"gmail SA key OK ({client_email})", extra
    except FileNotFoundError:
        return False, f"key file not found: {extra.get('key_path', '?')}", extra
    except Exception as exc:
        # invalid_grant and friends land here — this is the alert we missed.
        return False, f"gmail auth failed: {type(exc).__name__}: {str(exc)[:120]}", extra


def check_ringcentral_auth() -> tuple[bool, str, dict]:
    """Can we exchange the RingCentral JWT for an access token? Auth-only."""
    extra: dict = {}
    try:
        import base64
        import urllib.error
        import urllib.parse

        # Secret names mirror the phone-watchdog's ringcentral_client.py.
        client_id = _read_secret("ringcentral-accountability-client-id") or os.environ.get("RINGCENTRAL_CLIENT_ID", "")
        client_secret = _read_secret("ringcentral-accountability-client-secret") or os.environ.get("RINGCENTRAL_CLIENT_SECRET", "")
        jwt = _read_secret("ringcentral-accountability-jwt") or os.environ.get("RINGCENTRAL_JWT", "")
        server_url = (_read_secret("ringcentral-accountability-server-url")
                      or os.environ.get("RINGCENTRAL_SERVER_URL", "")
                      or "https://platform.ringcentral.com").rstrip("/")
        extra["server_url"] = server_url
        extra["creds_visible"] = bool(client_id and jwt)
        if not client_id or not jwt:
            return False, "ringcentral credentials not visible (Secret Manager + env)", extra

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        data = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        }).encode()
        req = urllib.request.Request(
            f"{server_url}/restapi/oauth/token", data=data, method="POST",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        if body.get("access_token"):
            return True, "ringcentral JWT exchange OK", extra
        return False, "ringcentral token endpoint returned no access_token", extra
    except urllib.error.HTTPError as exc:
        return False, f"ringcentral auth failed: HTTP {exc.code}", extra
    except Exception as exc:
        return False, f"ringcentral auth failed: {type(exc).__name__}: {str(exc)[:100]}", extra


def check_ezlynx_auth() -> tuple[bool, str, dict]:
    """Can we obtain an EZLynx OAuth token? Auth-only, no operations.

    Uses the released robie_job_engine client so config/secret handling stays
    in one place. Skips (not fails) when the EZLynx config isn't visible to the
    health-check environment — deploy must set ROBIE_ENV + the secret ref for
    this probe to activate.
    """
    extra: dict = {}
    try:
        _prepend_release_import()
        from robie_job_engine.ezlynx_api import load_ezlynx_api_config, EzlynxApiClient

        try:
            config = load_ezlynx_api_config()
        except Exception as exc:
            extra["skipped"] = True
            return True, f"ezlynx probe skipped (config not visible: {type(exc).__name__})", extra

        client = EzlynxApiClient(config)
        token = client.get_token()
        if token:
            return True, "ezlynx OAuth token OK", extra
        return False, "ezlynx token endpoint returned empty token", extra
    except Exception as exc:
        return False, f"ezlynx auth failed: {type(exc).__name__}: {str(exc)[:100]}", extra


def _journal_since(unit: str, since: str) -> str:
    """Return journal output for a unit since a relative time. Empty on failure."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", since, "--no-pager"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout
    except Exception:
        return ""


def check_sweep_freshness() -> tuple[bool, str, dict]:
    """Is each job producing successful runs on time?

    - Phone-watchdog (continuous ~5m loop): journal must show sweep activity
      in the trailing window.
    - Timer jobs: the timer must be active and its last trigger recent enough.
    """
    extra: dict = {}
    problems: list[str] = []

    # 1. Phone-watchdog continuous sweep.
    try:
        active_out = subprocess.run(
            ["systemctl", "is-active", "streetsmart-phone-watchdog.service"],
            capture_output=True, text=True, timeout=10,
        )
        wd_active = active_out.stdout.strip() == "active"
        extra["watchdog_active"] = wd_active
        if not wd_active:
            problems.append("phone-watchdog service is not active")
        else:
            log = _journal_since("streetsmart-phone-watchdog.service", "20 minutes ago")
            # Sweep markers from watchdog_phone_alerts.py
            markers = ["Sweep complete", "Fetched", "Queue watch", "Triaged Call"]
            recent = any(m in log for m in markers)
            extra["watchdog_recent_sweep"] = recent
            if not recent:
                problems.append(
                    f"phone-watchdog silent for 20m (no sweep markers in journal)"
                )
    except Exception as exc:
        problems.append(f"watchdog freshness check failed: {type(exc).__name__}")

    # 2. Timer jobs.
    for timer, max_age, desc in TIMER_FRESHNESS:
        try:
            out = subprocess.run(
                ["systemctl", "show", timer, "-p", "ActiveState", "-p", "LastTriggerUSec"],
                capture_output=True, text=True, timeout=10,
            )
            props = {}
            for line in out.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
            state = props.get("ActiveState", "unknown")
            last_trigger = props.get("LastTriggerUSec", "")
            extra[f"{timer}_state"] = state
            if state != "active":
                problems.append(f"{desc}: timer not active (state={state})")
                continue
            # LastTriggerUSec looks like "Sun 2026-09-27 11:00:00 EDT" or "n/a".
            if last_trigger.strip().lower() in ("n/a", "", "0"):
                problems.append(f"{desc}: timer never triggered")
                continue
            try:
                # Parse "Day YYYY-MM-DD HH:MM:SS TZ" (22 chars before the TZ).
                ts = datetime.strptime(last_trigger.strip()[:22], "%a %Y-%m-%d %H:%M:%S")
                age = (datetime.now() - ts).total_seconds()
                extra[f"{timer}_age_hours"] = round(age / 3600, 1)
                if age > max_age:
                    problems.append(f"{desc}: last trigger {age/3600:.1f}h ago (limit {max_age/3600:.0f}h)")
            except (ValueError, IndexError):
                extra[f"{timer}_last_trigger_unparsed"] = last_trigger[:40]
        except Exception as exc:
            problems.append(f"{desc}: freshness check failed: {type(exc).__name__}")

    if problems:
        return False, "; ".join(problems), extra
    return True, "all jobs fresh", extra


def check_service_errors() -> tuple[bool, str, dict]:
    """Any failure signatures in service journals in the trailing hour?"""
    extra: dict = {}
    problems: list[str] = []
    for svc in ERROR_SCAN_SERVICES:
        log = _journal_since(svc, "1 hour ago")
        if not log:
            extra[svc] = "no journal output"
            continue
        hits: dict[str, int] = {}
        for pat in ERROR_PATTERNS:
            n = log.count(pat)
            if n:
                hits[pat] = n
        extra[svc] = hits if hits else "clean"
        if hits:
            problems.append(f"{svc}: {', '.join(f'{p}×{n}' for p, n in hits.items())}")
    if problems:
        return False, "; ".join(problems), extra
    return True, "no failure signatures in trailing hour", extra


def _probe_gmail_key(key_path: str) -> tuple[bool, str, dict]:
    """Auth-only Gmail getProfile for one SA key file. Never logs secrets."""
    extra: dict = {"key_path": key_path}
    try:
        with open(key_path) as f:
            key_data = json.load(f)
        client_email = str(key_data.get("client_email", ""))
        extra["client_email"] = client_email
        if not key_data.get("private_key") or not client_email:
            return False, f"key file {key_path} is not a valid SA key", extra

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            subject=WATCHDOG_MAILBOX,
        )
        svc = build("gmail", "v1", credentials=creds)
        profile = svc.users().getProfile(userId="me").execute()
        extra["gmail_user"] = profile.get("emailAddress", "")
        return True, f"gmail SA key OK ({client_email})", extra
    except FileNotFoundError:
        return False, f"key file not found: {key_path}", extra
    except Exception as exc:
        return False, f"gmail auth failed: {type(exc).__name__}: {str(exc)[:120]}", extra


def check_phone_gmail_keys() -> tuple[bool, str, dict]:
    """Are BOTH phone-watchdog Gmail keys alive? Reported separately.

    The 2026-09-27 incident: the primary key died (invalid_grant) while the
    backup kept the daemon alive. Probing only the live key would have masked
    the primary's death. Each key gets its own verdict.
    """
    extra: dict = {"keys": {}}
    problems: list[str] = []
    for key_path, label in PHONE_GMAIL_KEYS:
        ok, detail, key_extra = _probe_gmail_key(key_path)
        extra["keys"][label] = {"ok": ok, "detail": detail,
                                "client_email": key_extra.get("client_email", "")}
        if not ok:
            problems.append(f"{label} ({key_path}): {detail}")
    if problems:
        return False, "; ".join(problems), extra
    return True, f"both phone Gmail keys OK ({len(PHONE_GMAIL_KEYS)} probed)", extra


def check_login_secret_states() -> tuple[bool, str, dict]:
    """Do the EZLynx login secrets have an ENABLED version? States only.

    Reuses robie_job_engine/login_secret_health.inspect_login_secrets, which
    lists version states without ever reading payloads. Alerts only when a
    watched secret has NO enabled version (a DESTROYED newest with an older
    ENABLED still present is healthy — see that module's docstring).
    """
    extra: dict = {}
    try:
        _prepend_release_import()
        from robie_job_engine.login_secret_health import inspect_login_secrets

        report = inspect_login_secrets()
    except Exception as exc:
        extra["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return False, f"secret state check failed: {type(exc).__name__}", extra

    result = report.get("result", "UNKNOWN")
    extra["result"] = result
    extra["project"] = report.get("project", "")
    for item in report.get("secrets", []):
        extra[item.get("secret_id", "?")] = {
            "enabled": item.get("enabled_versions", []),
            "newest_state": item.get("newest_state"),
            "alert": item.get("alert"),
        }
    if result == "OK":
        return True, report.get("reason", "each watched secret has an ENABLED version"), extra
    if result == "UNKNOWN":
        # Secret Manager unreachable — don't page, but don't claim healthy.
        return True, f"secret states UNKNOWN ({report.get('reason', '')[:80]})", extra
    return False, report.get("reason", "secret version alert"), extra


def check_applicant_ingest_freshness() -> tuple[bool, str, dict]:
    """Is the applicant phone-match export fresh? Warn 30h, fail 36h.

    Past the 36h cliff, unknown callers fail closed — no callback tasks are
    created. The 6h warning window gives time to fix the ingest before it
    bites.
    """
    extra: dict = {"path": APPLICANT_EXPORT_PATH}
    try:
        mtime = os.path.getmtime(APPLICANT_EXPORT_PATH)
    except FileNotFoundError:
        return False, f"applicant export missing: {APPLICANT_EXPORT_PATH}", extra
    except OSError as exc:
        return False, f"applicant export unreadable: {type(exc).__name__}", extra
    age = time.time() - mtime
    extra["age_hours"] = round(age / 3600, 1)
    extra["size_bytes"] = os.path.getsize(APPLICANT_EXPORT_PATH)
    if age >= APPLICANT_FAIL_SECONDS:
        return False, (
            f"applicant export {age/3600:.1f}h old (cliff is 36h) — "
            "unknown callers are failing closed"
        ), extra
    if age >= APPLICANT_WARN_SECONDS:
        # Warn but don't page: return ok=False only past the cliff. A warning
        # goes in the detail so the daily digest can surface it.
        extra["warning"] = True
        return True, (
            f"applicant export {age/3600:.1f}h old — WARNING: 36h cliff in "
            f"{(APPLICANT_FAIL_SECONDS - age)/3600:.1f}h"
        ), extra
    return True, f"applicant export {age/3600:.1f}h old", extra


def _most_recent_eod_date(now: datetime | None = None) -> datetime:
    """Return the date of the most recent expected EOD run.

    EOD runs Mon-Fri at 17:00 ET. Returns the most recent weekday (Mon-Fri)
    strictly before today: on Tue-Fri that's yesterday; on Mon/Sat/Sun
    that's Friday. Checking for "today's" file in the morning was the
    2026-09-29 false alarm — at 06:00, today's 17:00 run hasn't happened.
    """
    from datetime import timedelta
    now = now or datetime.now()
    d = now.date() - timedelta(days=1)
    while d.weekday() > 4:  # 5=Sat, 6=Sun → walk back to Friday
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day)


def _check_eod_sheet_in_drive(date_str: str, extra: dict) -> tuple[bool, str]:
    """Is there a Sheet named 'EOD Phone Report -- YYYY-MM-DD' in Drive?

    Returns (found, detail). Any Drive/API/credential problem returns
    (False, reason) with the problem recorded in extra — the caller decides
    whether to fail or fall back to the local file. Never raises.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        extra["drive_check"] = "skipped: google-api-python-client not installed"
        return False, "Drive API client not installed"

    folder_id = os.environ.get(EOD_SHEETS_FOLDER_ID_ENV, "").strip()
    if not os.path.isfile(EOD_SA_KEY_PATH):
        extra["drive_check"] = f"skipped: service key not found"
        return False, "service key not found"
    if not folder_id:
        extra["drive_check"] = f"skipped: {EOD_SHEETS_FOLDER_ID_ENV} not set"
        return False, f"{EOD_SHEETS_FOLDER_ID_ENV} not set"

    try:
        creds = service_account.Credentials.from_service_account_file(
            EOD_SA_KEY_PATH,
            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        svc = build("drive", "v3", credentials=creds)
        sheet_name = EOD_SHEET_NAME_FMT.format(date=date_str)
        # Escape single quotes for the Drive query language.
        safe_name = sheet_name.replace("'", "\'")
        q = (f"name='{safe_name}' and "
             f"mimeType='application/vnd.google-apps.spreadsheet' and "
             f"'{folder_id}' in parents and trashed=false")
        res = svc.files().list(
            q=q, corpora="drive", driveId=folder_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="files(id,name,modifiedTime)").execute()
        files = res.get("files", [])
        extra["drive_check"] = f"found {len(files)} Sheet(s) named '{sheet_name}'"
        if files:
            extra["sheet_id"] = files[0]["id"]
            extra["sheet_modified"] = files[0].get("modifiedTime", "")
            return True, f"Sheet '{sheet_name}' found in Drive"
        return False, f"Sheet '{sheet_name}' not found in Drive"
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        extra["drive_check"] = f"error: {type(exc).__name__}: {str(exc)[:100]}"
        return False, f"Drive check failed: {type(exc).__name__}"


def check_eod_drive_delivery() -> tuple[bool, str, dict]:
    """Did the most recent EOD phone report land as a Google Sheet?

    The EOD runs Mon-Fri at 17:00 ET. This confirms the OUTCOME: the most
    recent expected run's report exists as a Google Sheet in the Shared
    Drive. The 2026-09-29 false alarm checked for "today's" Excel at 06:00 —
    today's 17:00 run can't have happened yet. The Sheet is the outcome now
    (Excel+Drive upload was replaced 2026-09-28); the local file is only a
    fallback when Drive is unreachable.
    """
    extra: dict = {}
    expected = _most_recent_eod_date()
    date_str = expected.strftime("%Y-%m-%d")      # Sheet name format
    date_compact = expected.strftime("%Y%m%d")    # legacy Excel format
    extra["expected_date"] = date_str

    # Primary: the Google Sheet in the Shared Drive (the outcome).
    found, detail = _check_eod_sheet_in_drive(date_str, extra)
    if found:
        return True, f"EOD report for {date_str} delivered as Google Sheet", extra

    # Fallback: local Excel. Only fail if the local file is ALSO missing —
    # Drive being temporarily unreachable shouldn't page when the report ran.
    try:
        names = os.listdir(EOD_OUTPUT_DIR)
    except (FileNotFoundError, OSError) as exc:
        extra["local_fallback"] = f"unreadable: {type(exc).__name__}"
        return False, (
            f"Yesterday's EOD phone report ({date_str}) is missing: no Google "
            f"Sheet in Drive ({detail}) and the local output folder is "
            f"unreadable. The 5 PM run may not have completed."
        ), extra

    xlsx = f"eod_phone_report_{date_compact}.xlsx"
    md = f"eod_phone_leakage_{date_compact}.md"
    extra["xlsx_present"] = xlsx in names
    extra["md_present"] = md in names
    if xlsx in names or md in names:
        extra["local_fallback"] = "used"
        return True, (
            f"EOD report for {date_str} present locally; "
            f"Drive Sheet check: {detail}"
        ), extra

    return False, (
        f"Yesterday's EOD phone report ({date_str}) is missing: no Google "
        f"Sheet in Drive ({detail}) and no local file ({xlsx}). "
        f"The 5 PM run may not have completed."
    ), extra


def check_task_verifier_health() -> tuple[bool, str, dict]:
    """Is the task verifier keeping up? Any tasks stuck unverified?

    The verifier runs every 15m and checks queued phone-watchdog tasks
    (delivered or sent_to_relay handoffs) against a fresh EZLynx report.
    sent_to_relay is not proof the task landed. Tasks stuck
    PENDING/UNVERIFIED for 2h+ mean the verifier is broken or EZLynx is
    unreachable — either way, callback tasks may be silently missing.
    """
    extra: dict = {}
    db_path = os.environ.get("ROBIE_TASK_VERIFY_DB", TASK_VERIFY_DB_DEFAULT)
    extra["db_path"] = db_path
    problems: list[str] = []
    try:
        import sqlite3

        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "pending_tasks" not in tables:
                return False, f"verifier DB missing pending_tasks table: {db_path}", extra
            cutoff = time.time() - TASK_VERIFY_STUCK_SECONDS
            # created_at is ISO text; compare as epoch via strftime. The CAST
            # is required: strftime returns TEXT and SQLite sorts INTEGER
            # before TEXT, so an uncast comparison never matches.
            rows = conn.execute(
                """SELECT COUNT(*), MAX(created_at) FROM pending_tasks
                   WHERE status IN ('PENDING', 'UNVERIFIED')
                     AND CAST(strftime('%s', created_at) AS INTEGER) < ?""",
                (int(cutoff),),
            ).fetchone()
            stuck_count = rows[0] or 0
            extra["stuck_count"] = stuck_count
            extra["oldest_stuck"] = rows[1]
            if stuck_count:
                problems.append(
                    f"{stuck_count} task(s) stuck PENDING/UNVERIFIED for 2h+ "
                    f"(oldest {rows[1]})"
                )
            total = conn.execute(
                "SELECT COUNT(*) FROM pending_tasks WHERE status IN ('PENDING','UNVERIFIED')"
            ).fetchone()[0]
            extra["open_count"] = total
        finally:
            conn.close()
    except Exception as exc:
        return False, f"verifier DB unreadable: {type(exc).__name__}: {str(exc)[:80]}", extra

    # Also check the verifier service's own journal for failures.
    log = _journal_since("robie-task-verifier.service", "1 hour ago")
    if log:
        for pat in ("Traceback (most recent call last)", "OperationalError", "unable to open database"):
            if pat in log:
                problems.append(f"verifier journal shows '{pat}' in trailing hour")
                break
    extra["journal_checked"] = bool(log)
    if problems:
        return False, "; ".join(problems), extra
    return True, f"verifier healthy ({extra.get('open_count', 0)} open, none stuck 2h+)", extra


def _most_recent_tuesday(now: datetime) -> datetime:
    """Return the most recent Tuesday 08:00 ET (the 4359 fire time)."""
    # Tuesday is weekday 1. If today is Tuesday before 08:00 ET, the most
    # recent run is last Tuesday.
    from datetime import timedelta
    days_back = (now.weekday() - 1) % 7
    candidate = now.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    if days_back == 0 and now.hour < 8:
        candidate -= timedelta(days=7)
    return candidate


def check_4359_tuesday_proof() -> tuple[bool, str, dict]:
    """Did the most recent Tuesday 4359 run actually send (or explicitly hold)?

    Checks evidence-latest.json: ran_at must be after the most recent Tuesday
    08:00 ET, and it must show sent>0 or an explicit reason for 0 (held,
    dry-run, no qualifying rows). A timer that fired but sent nothing with no
    reason is a silent failure.
    """
    extra: dict = {"path": EVIDENCE_4359_PATH}
    try:
        with open(EVIDENCE_4359_PATH) as f:
            ev = json.load(f)
    except FileNotFoundError:
        return False, f"4359 evidence missing: {EVIDENCE_4359_PATH}", extra
    except PermissionError:
        # The 2026-09-29 false alarm: the worker wrote evidence-latest.json
        # mode 600 owned by streetsmart-hermes; the probe (different user)
        # couldn't read it. The worker now writes 640; the health-check user
        # must be in the file's group. This message says exactly that.
        return False, (
            f"4359 evidence not readable (permission denied): "
            f"{EVIDENCE_4359_PATH}. The worker should write it group-readable "
            f"(640) and the health-check user must be in the file's group."
        ), extra
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"4359 evidence unreadable: {type(exc).__name__}", extra

    ran_at_raw = ev.get("ran_at", "")
    extra["ran_at"] = ran_at_raw
    try:
        ran_at = datetime.fromisoformat(str(ran_at_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False, f"4359 evidence has unparsable ran_at: {ran_at_raw!r:.40}", extra

    now = datetime.now(timezone.utc)
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)
    cutoff = _most_recent_tuesday(now)
    # Compare in UTC; the 08:00 ET fire is 12:00/13:00 UTC — use a 6h grace.
    from datetime import timedelta
    grace_cutoff = cutoff - timedelta(hours=6)
    extra["cutoff_utc"] = grace_cutoff.isoformat()
    if ran_at < grace_cutoff:
        return False, (
            f"4359 evidence stale: last run {ran_at_raw} is before the most "
            f"recent Tuesday fire ({cutoff.date()})"
        ), extra

    summary = ev.get("summary", {}) or {}
    detail = ev.get("detail", {}) or {}
    sent = summary.get("sent", detail.get("sent", 0))
    extra["sent"] = sent
    extra["succeeded"] = ev.get("succeeded")
    if not ev.get("succeeded", True):
        return False, f"4359 last run reported failure: {ev.get('error', '?')[:100]}", extra
    if sent and int(sent) > 0:
        return True, f"4359 Tuesday proof: {sent} email(s) sent ({ran_at_raw})", extra
    # sent == 0: need an explicit reason, not silence.
    reason = (ev.get("hold_status") or detail.get("hold_reason")
              or ev.get("dry_run_note") or summary.get("note") or "")
    extra["zero_reason"] = str(reason)[:120]
    if reason:
        return True, f"4359 Tuesday: 0 sent, reason given: {str(reason)[:80]}", extra
    return False, "4359 Tuesday: 0 emails sent with no reason recorded", extra


def check_chat_intake() -> tuple[bool, str, dict]:
    """Is the Hermes Chat listener up?

    Reuses robie_job_engine/production_preflight.check_chat_intake. Connected
    is read from the gateway log and the journal. A quiet inbox is INFO while
    the listener is connected and hermes-gateway is active. Fails when the
    gateway is inactive, the latest marker is a disconnect or error, or
    neither source has a connect marker.
    """
    extra: dict = {}
    try:
        _prepend_release_import()
        from robie_job_engine.production_preflight import check_chat_intake as _preflight_check

        result = _preflight_check()
    except Exception as exc:
        extra["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return False, f"chat intake check failed: {type(exc).__name__}", extra
    extra["evidence"] = result.get("evidence", "")
    if result.get("severity"):
        extra["severity"] = result.get("severity")
    if result.get("ok"):
        return True, result.get("evidence", "chat intake live"), extra
    return False, result.get("evidence", "chat intake problem"), extra


def _last_preflight_startup(journal: str) -> str | None:
    found = None
    for line in str(journal or "").splitlines():
        if "preflight startup:" in line and "ROBIE_CHAT_SA_KEY_FILE=" in line:
            found = line.strip()
    return found


def check_preflight_alert_delivery(journal: str | None = None) -> tuple[bool, str, dict]:
    """Can the hourly preflight actually deliver its Chat alert?

    Reads the preflight unit journal. Missing output is not a failure (the
    probe cannot see a run). Fails when the latest result has
    ``alert_delivery_failed`` true, or when the latest startup line shows
    ``ROBIE_CHAT_SA_KEY_FILE`` unset — a green run never posts, so the unset
    key would otherwise stay invisible until the next real no.
    """
    extra: dict = {}
    try:
        if journal is None:
            journal = _journal_since("robie-production-preflight.service", "36 hours ago")
        _prepend_release_import()
        from robie_job_engine.production_preflight import parse_preflight_alert_state

        text = journal or ""
        parsed = parse_preflight_alert_state(text)
        startup = _last_preflight_startup(text)
    except Exception as exc:
        extra["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        return True, f"preflight alert delivery not visible: {type(exc).__name__}", extra
    if startup:
        extra["startup"] = startup[-240:]
    if parsed:
        extra["alert_delivery_failed"] = parsed.get("alert_delivery_failed")
        error = parsed.get("chat_post_error")
        if error:
            extra["chat_post_error"] = str(error)[:200]
        if parsed.get("alert_delivery_failed") is True:
            detail = str(error or "chat post failed")[:200]
            return False, f"preflight alerts broken: {detail}", extra
    if startup and "ROBIE_CHAT_SA_KEY_FILE=(unset)" in startup:
        return False, (
            "preflight alerts broken: ROBIE_CHAT_SA_KEY_FILE is unset"
        ), extra
    if not parsed and not startup:
        return True, "no preflight JSON in recent journal", extra
    return True, "preflight alert delivery ok", extra


def check_ascend_driver_stall() -> tuple[bool, str, dict]:
    """Alert when actionable Ascend notices were seen and nothing was filed.

    Reads ``/var/lib/robie-ascend-notice-driver/runs.jsonl`` (or
    ``ASCEND_DRIVER_STATE_DIR`` / ``ASCEND_DRIVER_RUN_LOG``). A missing log
    or fewer than four completed live runs is quiet. Dry runs do not count.
    Ignored and unrecognized notices do not count as actionable. The
    health-check user on Production cannot read the system journal, so
    this probe does not call journalctl.
    """
    extra: dict = {}
    try:
        _prepend_release_import()
        from robie_job_engine.ascend_driver_stall import evaluate_stall_file

        verdict = evaluate_stall_file()
    except Exception as exc:
        extra["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return True, f"ascend driver stall not visible: {type(exc).__name__}", extra
    extra.update(verdict)
    if verdict.get("status") == "ALERT":
        return False, str(verdict.get("detail") or "ascend driver stalled"), extra
    return True, str(verdict.get("detail") or "ascend driver not stalled"), extra


def check_duplicate_guard() -> tuple[bool, str, dict]:
    """Does the outbound duplicate guard still behave correctly?

    Outcome probe (PR #735): a correction — same thread, same recipient and
    subject, but DIFFERENT body — must NOT be skipped as a duplicate, while
    an exact duplicate (identical body) MUST still be skipped. Uses a fake
    Gmail service; no network, no sends. Fails if the guard regresses to
    the pre-#735 thread-blind behavior or stops skipping true duplicates.
    """
    extra: dict = {}
    try:
        from robie_job_engine.outbound_send_guard import should_skip_send
    except Exception as exc:
        return False, f"cannot import should_skip_send: {type(exc).__name__}", extra

    import base64 as _b64
    import time as _time

    def _b64e(text: str) -> str:
        return _b64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")

    def _msg(mid, to, subject, body, labels, internal_ms):
        return {
            "id": mid,
            "threadId": "health-probe-thread",
            "labelIds": labels,
            "internalDate": str(internal_ms),
            "payload": {
                "mimeType": "text/plain",
                "body": {"data": _b64e(body)},
                "headers": [
                    {"name": "To", "value": to},
                    {"name": "Subject", "value": subject},
                ],
            },
        }

    class _FakeMessages:
        def __init__(self, svc):
            self.svc = svc

        def list(self, userId=None, q=None, maxResults=None, **kw):
            return self

        def get(self, userId=None, id=None, format=None,
                metadataHeaders=None, **kw):
            self.svc._last_get = {"id": id, "format": format}
            return self

        def execute(self):
            if self.svc._last_get and self.svc._last_get.get("format") == "metadata":
                return self.svc.sent_meta[self.svc._last_get["id"]]
            if self.svc._last_get:
                return self.svc.by_id[self.svc._last_get["id"]]
            return {"messages": [{"id": m["id"]} for m in self.svc.sent_index]}

    class _FakeThreads:
        def __init__(self, svc):
            self.svc = svc

        def get(self, userId=None, id=None, format=None, **kw):
            return self

        def execute(self):
            return {"messages": self.svc.thread_messages}

    class _FakeUsers:
        def __init__(self, svc):
            self.svc = svc

        def messages(self):
            return _FakeMessages(self.svc)

        def threads(self):
            return _FakeThreads(self.svc)

    class _FakeGmail:
        def __init__(self, sent_index, sent_meta, by_id, thread_messages):
            self.sent_index = sent_index
            self.sent_meta = sent_meta
            self.by_id = by_id
            self.thread_messages = thread_messages
            self._last_get = None

        def users(self):
            return _FakeUsers(self)

    def _meta(mid, to, subject, internal_ms):
        return {
            "id": mid,
            "internalDate": str(internal_ms),
            "payload": {
                "headers": [
                    {"name": "To", "value": to},
                    {"name": "Subject", "value": subject},
                ]
            },
        }

    try:
        now_ms = int(_time.time() * 1000)
        to = "robie@streetsmart.insurance"
        subject = "Re: health-probe"
        original = "original request body"
        correction = "correction: different body content here"
        reply = "robie reply body"

        thread = [
            _msg("m-orig", to, subject, original, ["INBOX"], now_ms - 7200_000),
            _msg("m-reply", "probe@streetsmart.insurance", subject, reply,
                 ["SENT"], now_ms - 3600_000),
            _msg("m-corr", to, subject, correction, ["INBOX"], now_ms - 600_000),
        ]
        sent_meta = {
            "m-reply": _meta("m-reply", "probe@streetsmart.insurance",
                             subject, now_ms - 3600_000),
        }
        by_id = {m["id"]: m for m in thread}
        svc = _FakeGmail([{"id": "m-reply"}], sent_meta, by_id, thread)

        # 1. Correction must NOT be skipped.
        skip_corr, reason_corr = should_skip_send(
            svc, "probe@streetsmart.insurance", subject,
            incoming_body=correction, thread_id="health-probe-thread",
        )
        extra["correction"] = {"skip": skip_corr, "reason": reason_corr}
        if skip_corr:
            return False, (
                "duplicate guard REGRESSED: correction skipped as duplicate "
                f"({reason_corr})"
            ), extra

        # 2. Exact duplicate MUST still be skipped.
        svc2 = _FakeGmail([{"id": "m-reply"}], sent_meta, by_id, thread[:2])
        skip_dup, reason_dup = should_skip_send(
            svc2, "probe@streetsmart.insurance", subject,
            incoming_body=original, thread_id="health-probe-thread",
        )
        extra["duplicate"] = {"skip": skip_dup, "reason": reason_dup}
        if not skip_dup:
            return False, (
                "duplicate guard DISABLED: exact duplicate was not skipped "
                f"({reason_dup})"
            ), extra

        return True, (
            "correction processed, exact duplicate skipped"
        ), extra
    except Exception as exc:
        return False, f"probe errored: {type(exc).__name__}: {exc}", extra


def _systemctl_value(command: str, unit: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", command, unit],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception as exc:
        return f"error:{type(exc).__name__}"


def _require_active_unit(unit: str) -> tuple[bool, str, dict]:
    state = _systemctl_value("is-active", unit)
    extra = {"unit": unit, "state": state}
    if state == "active":
        return True, f"{unit} active", extra
    return False, f"{unit} is {state}", extra


def check_test_gateway() -> tuple[bool, str, dict]:
    """Is the Test gateway unit the deploy workflow actually restarts active?"""
    return _require_active_unit(TEST_GATEWAY_UNIT)


def check_test_ezlynx_browser() -> tuple[bool, str, dict]:
    """Is the Test EZLynx Chrome unit active?"""
    return _require_active_unit(TEST_BROWSER_UNIT)


def check_test_keepalive() -> tuple[bool, str, dict]:
    """Is the Test keepalive timer scheduled, and the oneshot not failed?"""
    timer_state = _systemctl_value("is-active", TEST_KEEPALIVE_TIMER)
    service_state = _systemctl_value("is-failed", TEST_KEEPALIVE_SERVICE)
    extra = {"timer": timer_state, "service": service_state}
    problems: list[str] = []
    if timer_state != "active":
        problems.append(f"{TEST_KEEPALIVE_TIMER} is {timer_state}")
    if service_state in {"failed", "not-found"} or service_state.startswith("error:"):
        problems.append(f"{TEST_KEEPALIVE_SERVICE} is {service_state}")
    if problems:
        return False, "; ".join(problems), extra
    return True, f"{TEST_KEEPALIVE_TIMER} active", extra


def check_test_paths() -> tuple[bool, str, dict]:
    """Do the Test release pointer and jobs database exist?"""
    current = os.path.join(TEST_ROOT, "current")
    required = [TEST_ROOT, TEST_RELEASE_ROOT, TEST_JOBS_DB]
    missing = [path for path in required if not os.path.exists(path)]
    extra: dict = {"missing": missing, "current": current}
    if missing:
        return False, "missing Test path(s): " + ", ".join(missing), extra
    if os.path.islink(current) and os.path.islink(TEST_RELEASE_ROOT):
        if os.path.realpath(current) != os.path.realpath(TEST_RELEASE_ROOT):
            return False, "Test current and releases/current point at different trees", extra
    return True, "Test root, release pointer, and jobs.db present", extra


def check_test_service_errors() -> tuple[bool, str, dict]:
    """Failure signatures in Test gateway, browser, and keepalive journals."""
    extra: dict = {}
    problems: list[str] = []
    for svc in TEST_ERROR_SCAN_SERVICES:
        log = _journal_since(svc, "1 hour ago")
        if not log:
            extra[svc] = "no journal output"
            continue
        hits: dict[str, int] = {}
        for pat in ERROR_PATTERNS:
            count = log.count(pat)
            if count:
                hits[pat] = count
        extra[svc] = hits if hits else "clean"
        if hits:
            problems.append(f"{svc}: {', '.join(f'{pat}×{count}' for pat, count in hits.items())}")
    if problems:
        return False, "; ".join(problems), extra
    return True, "no failure signatures in Test units", extra


def check_test_stuck_job_leases() -> tuple[bool, str, dict]:
    """A Test job still RUNNING / leased after an hour is a fault."""
    extra: dict = {"db": TEST_JOBS_DB}
    if not os.path.isfile(TEST_JOBS_DB):
        return False, f"Test jobs.db missing: {TEST_JOBS_DB}", extra
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{TEST_JOBS_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                """SELECT id, status, updated_at FROM jobs
                   WHERE status IN ('RUNNING', 'VERIFYING', 'AWAITING_HUMAN_INPUT')"""
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return False, f"Test jobs.db unreadable: {type(exc).__name__}", extra
    now = datetime.now(timezone.utc)
    stuck: list[str] = []
    for job_id, status, updated_at in rows:
        stamp = None
        if updated_at:
            try:
                stamp = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            except ValueError:
                stamp = None
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = None if stamp is None else (now - stamp).total_seconds()
        if age is None or age > STUCK_LEASE_SECONDS:
            stuck.append(f"{job_id}:{status}")
    extra["stuck"] = stuck
    if stuck:
        return False, f"{len(stuck)} Test job(s) holding the browser for over an hour", extra
    return True, "no stuck Test job leases", extra


def check_ascend_unmatched_digest() -> tuple[bool, str, dict]:
    """Alert when the accounting digest failed or skipped a business day.

    Reads the digest's last-run file (or ``ASCEND_UNMATCHED_DIGEST_STATE``).
    A missing file is quiet: the unit is not installed yet. This probe does
    not read the journal and does not send email.
    """
    extra: dict = {}
    try:
        _prepend_release_import()
        from robie_job_engine.ascend_unmatched_digest import evaluate_digest_health

        verdict = evaluate_digest_health()
    except Exception as exc:
        extra["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return True, f"unmatched digest not visible: {type(exc).__name__}", extra
    extra.update(verdict)
    if verdict.get("ok"):
        return True, str(verdict.get("detail") or "unmatched digest ok"), extra
    return False, str(verdict.get("detail") or "unmatched digest needs attention"), extra


# Probes that look at Production phone, EOD, 4359, Chat, and the Production
# preflight unit. A Test run skips these. Shared probes (disk, code version,
# EZLynx auth skip, duplicate guard) stay.
PROD_ONLY_CHECK_NAMES = frozenset({
    "worker_alive",
    "env_vars",
    "hitl_dry_run",
    "gmail_sa_key",
    "ringcentral_auth",
    "sweep_freshness",
    "service_errors",
    "phone_gmail_keys",
    "applicant_ingest_freshness",
    "eod_drive_delivery",
    "task_verifier_health",
    "tuesday_4359_proof",
    "chat_intake",
    "preflight_alert_delivery",
    "ascend_driver_stall",
    "ascend_unmatched_digest",
})

TEST_CHECKS = [
    ("test_gateway", check_test_gateway),
    ("test_ezlynx_browser", check_test_ezlynx_browser),
    ("test_keepalive", check_test_keepalive),
    ("test_paths", check_test_paths),
    ("test_service_errors", check_test_service_errors),
    ("test_stuck_job_leases", check_test_stuck_job_leases),
]


def checks_for_profile(profile: str | None = None) -> list[tuple]:
    """Production returns CHECKS unchanged. TEST drops Prod-only probes."""
    chosen = resolve_health_profile(profile)
    if chosen != "TEST":
        return list(CHECKS)
    selected = [(name, fn) for name, fn in CHECKS if name not in PROD_ONLY_CHECK_NAMES]
    selected.extend(TEST_CHECKS)
    return selected


CHECKS = [
    ("worker_alive", check_worker_alive),
    ("code_version", check_code_version),
    ("env_vars", check_env_vars),
    ("hitl_dry_run", check_hitl_dry_run),
    ("stuck_leases", check_stuck_leases),
    ("disk", check_disk),
    ("gmail_sa_key", check_gmail_sa_key),
    ("ringcentral_auth", check_ringcentral_auth),
    ("ezlynx_auth", check_ezlynx_auth),
    ("sweep_freshness", check_sweep_freshness),
    ("service_errors", check_service_errors),
    # Systems watchdog phases 2–3 (2026-09-28).
    ("phone_gmail_keys", check_phone_gmail_keys),
    ("login_secret_states", check_login_secret_states),
    ("applicant_ingest_freshness", check_applicant_ingest_freshness),
    ("eod_drive_delivery", check_eod_drive_delivery),
    ("task_verifier_health", check_task_verifier_health),
    ("tuesday_4359_proof", check_4359_tuesday_proof),
    ("chat_intake", check_chat_intake),
    ("preflight_alert_delivery", check_preflight_alert_delivery),
    ("ascend_driver_stall", check_ascend_driver_stall),
    ("duplicate_guard", check_duplicate_guard),
    ("ascend_unmatched_digest", check_ascend_unmatched_digest),
]


# ---------------------------------------------------------------------------
# Chat alert (failure only) + daily digest (explicit all-green)
# ---------------------------------------------------------------------------

def send_chat_alert(failures: list[dict], profile: str = "PRODUCTION") -> bool:
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    title = "🚨 *ROBIE health check FAILED*"
    if profile == "TEST":
        title = "🚨 *ROBIE Test health check FAILED*"
    lines = [title]
    for f in failures:
        lines.append(f"• *{f['name']}*: {f['detail']}")
    lines.append(f"_{_now_iso()}_")
    body = json.dumps({"text": "\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send_daily_digest(results: list[dict]) -> bool:
    """Post the morning all-green digest (or a failure summary).

    Phase 3: silence-means-healthy is replaced by explicit confirmation. When
    run with --daily-digest (intended for a ~07:00 ET timer, Dusty's lane),
    this posts one concise Chat message every morning: either "all N checks
    green" or the failure list. The hourly failure-only alerts are unchanged.
    """
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    failures = [r for r in results if not r["ok"]]
    warnings = [r for r in results
                if r["ok"] and r.get("extra", {}).get("warning")]
    if failures:
        lines = [f"🌅 *ROBIE morning digest — {len(failures)} issue(s)*"]
        for f in failures:
            lines.append(f"• *{f['name']}*: {f['detail']}")
    else:
        lines = [f"✅ *ROBIE morning digest — all {len(results)} checks green*"]
    for w in warnings:
        lines.append(f"⚠️ *{w['name']}*: {w['detail']}")
    # One-line rollup of the proof checks Carlo cares about.
    proof = {r["name"]: r["detail"] for r in results
             if r["name"] in ("tuesday_4359_proof", "eod_drive_delivery",
                              "applicant_ingest_freshness", "chat_intake")}
    for name, detail in proof.items():
        lines.append(f"  _{name}: {detail[:90]}_")
    lines.append(f"_{_now_iso()}_")
    body = json.dumps({"text": "\n".join(lines)}).encode()
    try:
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def execute(
    *,
    profile: str,
    status_dir: str,
    no_chat: bool,
    daily_digest: bool,
) -> int:
    global _REQUESTED_PROFILE
    previous_profile = _REQUESTED_PROFILE
    _REQUESTED_PROFILE = profile
    results: list[dict] = []
    try:
        for name, fn in checks_for_profile(profile):
            try:
                ok, detail, extra = fn()
            except Exception as exc:  # a check must never kill the run
                ok, detail, extra = False, f"check crashed: {type(exc).__name__}: {exc}", {}
            results.append({
                "name": name, "ok": ok, "detail": detail,
                "extra": extra, "at": _now_iso(),
            })
    except Exception as exc:
        print(f"health check framework error: {exc}", file=sys.stderr)
        return 2
    finally:
        _REQUESTED_PROFILE = previous_profile

    failures = [r for r in results if not r["ok"]]
    healthy = not failures

    status = {
        "at": _now_iso(),
        "profile": profile,
        "healthy": healthy,
        "failure_count": len(failures),
        "checks": results,
    }
    try:
        os.makedirs(status_dir, exist_ok=True)
        with open(os.path.join(status_dir, "status.json"), "w") as f:
            json.dump(status, f, indent=2)
    except Exception as exc:
        print(f"could not write status file: {exc}", file=sys.stderr)

    # Human-readable summary to stdout (goes to cron log).
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"[{mark}] {r['name']}: {r['detail']}", flush=True)

    if failures and not no_chat:
        sent = send_chat_alert(failures, profile=profile)
        print(f"chat alert sent: {sent}", flush=True)

    if daily_digest and not no_chat:
        digest_sent = send_daily_digest(results)
        print(f"daily digest sent: {digest_sent}", flush=True)

    return 0 if healthy else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ROBIE hourly health check")
    ap.add_argument("--status-dir", default="/tmp/robie-health",
                    help="where to write status.json")
    ap.add_argument("--no-chat", action="store_true",
                    help="never send Chat alerts (status file only)")
    ap.add_argument("--daily-digest", action="store_true",
                    help="post the morning all-green/failure digest to Chat "
                         "(intended for a daily ~07:00 ET timer; Dusty's lane)")
    ap.add_argument("--profile", choices=["PRODUCTION", "TEST"], default=None,
                    help="check profile; default follows ROBIE_ENV, else PRODUCTION")
    args = ap.parse_args()
    profile = resolve_health_profile(args.profile)
    return execute(
        profile=profile,
        status_dir=args.status_dir,
        no_chat=args.no_chat,
        daily_digest=args.daily_digest,
    )


if __name__ == "__main__":
    sys.exit(main())
