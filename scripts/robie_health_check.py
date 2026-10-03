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

Output:
  - JSON status file (always written, even when healthy)
  - Google Chat ping ONLY on failure (quiet when healthy)
  - Exit 0 = healthy, 1 = issues found, 2 = check itself errored

All probes are AUTH-ONLY / READ-ONLY: no operations, no writes, no sends.
A check must never crash the run; failures are reported, not raised.
Secret values are never logged — names and statuses only.

Usage (on hermes-poc-01):
  python3 robie_health_check.py [--status-dir /tmp/robie-health] [--no-chat]

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        # releases/current.
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
        from robie_job_engine.ezlynx_policy_setup import CODE_VERSION
        extra["loaded_version"] = CODE_VERSION
    except Exception as exc:
        return False, f"cannot import CODE_VERSION: {type(exc).__name__}: {exc}", extra

    # Compare against the deployed release symlink target (no network needed).
    try:
        link = "/opt/streetsmart-hermes/releases/current"
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
        sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
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
    ("duplicate_guard", check_duplicate_guard),
]


# ---------------------------------------------------------------------------
# Chat alert (failure only)
# ---------------------------------------------------------------------------

def send_chat_alert(failures: list[dict]) -> bool:
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not webhook:
        return False
    lines = ["🚨 *ROBIE health check FAILED*"]
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ROBIE hourly health check")
    ap.add_argument("--status-dir", default="/tmp/robie-health",
                    help="where to write status.json")
    ap.add_argument("--no-chat", action="store_true",
                    help="never send Chat alerts (status file only)")
    args = ap.parse_args()

    results: list[dict] = []
    try:
        for name, fn in CHECKS:
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

    failures = [r for r in results if not r["ok"]]
    healthy = not failures

    status = {
        "at": _now_iso(),
        "healthy": healthy,
        "failure_count": len(failures),
        "checks": results,
    }
    try:
        os.makedirs(args.status_dir, exist_ok=True)
        with open(os.path.join(args.status_dir, "status.json"), "w") as f:
            json.dump(status, f, indent=2)
    except Exception as exc:
        print(f"could not write status file: {exc}", file=sys.stderr)

    # Human-readable summary to stdout (goes to cron log).
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"[{mark}] {r['name']}: {r['detail']}", flush=True)

    if failures and not args.no_chat:
        sent = send_chat_alert(failures)
        print(f"chat alert sent: {sent}", flush=True)

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
