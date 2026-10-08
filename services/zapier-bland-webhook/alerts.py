"""Production reliability: failure alerting, kill switch, double-label guard.

All state here is in-memory (per Cloud Run instance). This is deliberate:
alerts are best-effort operational signals, not durable records. The EZLynx
writeback remains the system of record for every dispatch outcome.
"""
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("bland_dispatcher.alerts")

CHAT_WEBHOOK_ENV = "ROBIE_HEALTH_CHAT_WEBHOOK"

# Failure alerting: N failures inside WINDOW_S triggers one chat alert,
# then the window resets (no spam).
FAILURE_ALERT_THRESHOLD = 3
FAILURE_WINDOW_S = 15 * 60

# Double-label guard: two DIFFERENT campaigns for the same applicant inside
# this window get a chat heads-up (both calls still go out).
DOUBLE_LABEL_WINDOW_S = 5 * 60

# Kill switch: Secret Manager secret checked per dispatch, cached briefly
# so we don't hammer the Secret Manager API on every webhook.
KILL_SWITCH_SECRET = "bland-dispatcher-kill-switch"
KILL_SWITCH_CACHE_S = 60


def post_chat_alert(text: str) -> bool:
    """POST a plain-English alert to the ROBIE health Chat.

    Returns True if the POST succeeded. Never raises — alerting must not
    break the dispatch path.
    """
    webhook = os.environ.get(CHAT_WEBHOOK_ENV, "")
    message = f"*Bland dispatcher*\n{text}"
    if not webhook:
        logger.warning("chat alert not posted (no %s): %s", CHAT_WEBHOOK_ENV, text)
        return False
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.warning("chat alert POST returned %s", resp.status)
            return ok
    except Exception as e:
        logger.warning("chat alert POST failed: %s", e)
        return False


class FailureTracker:
    """Sliding-window failure counter with one-alert-per-window semantics."""

    def __init__(self, threshold: int = FAILURE_ALERT_THRESHOLD,
                 window_s: int = FAILURE_WINDOW_S):
        self.threshold = threshold
        self.window_s = window_s
        self._failures: list = []  # (timestamp, applicant_id, campaign_id, error)
        self._lock = threading.Lock()
        self._last_alert_at = 0.0

    def record(self, applicant_id: str, campaign_id: str, error: str) -> bool:
        """Record a failure. Returns True if this recording triggered an alert."""
        now = time.time()
        with self._lock:
            self._failures.append((now, applicant_id, campaign_id, error))
            # Prune outside the window.
            cutoff = now - self.window_s
            self._failures = [f for f in self._failures if f[0] >= cutoff]
            count = len(self._failures)
            if count >= self.threshold and now - self._last_alert_at >= self.window_s:
                self._last_alert_at = now
                last = self._failures[-1]
                text = (
                    f"Bland dispatcher: {count} failures in last 15 min. "
                    f"Last error: {last[3]}. "
                    f"Campaign: {last[2]}, Applicant: {last[1]}."
                )
                # Reset after alerting so we don't spam.
                self._failures = []
                # Post outside the lock to avoid holding it during I/O.
                alert = True
            else:
                alert = False
                text = ""
        if alert:
            post_chat_alert(text)
        return alert

    def reset_for_tests(self):
        with self._lock:
            self._failures = []
            self._last_alert_at = 0.0


# Module-level singleton used by app.py.
failures = FailureTracker()


class DoubleLabelTracker:
    """Detect two different campaigns for one applicant in a short window."""

    def __init__(self, window_s: int = DOUBLE_LABEL_WINDOW_S):
        self.window_s = window_s
        self._seen: dict = {}  # applicant_id -> list of (timestamp, campaign_id)
        self._lock = threading.Lock()

    def check(self, applicant_id: str, campaign_id: str) -> Optional[str]:
        """Record this dispatch. Returns an alert message if a DIFFERENT
        campaign fired for the same applicant inside the window, else None."""
        now = time.time()
        with self._lock:
            entries = self._seen.get(applicant_id, [])
            entries = [(ts, c) for ts, c in entries if now - ts < self.window_s]
            other = [c for _, c in entries if c != campaign_id]
            entries.append((now, campaign_id))
            self._seen[applicant_id] = entries
            if other:
                return (
                    f"Two different labels fired for applicant {applicant_id} "
                    f"within 5 min: {other[0]}, {campaign_id}. Both calls placed."
                )
            return None

    def reset_for_tests(self):
        with self._lock:
            self._seen = {}


# Module-level singleton used by app.py.
double_labels = DoubleLabelTracker()


# ---------------------------------------------------------------------------
# Instant kill switch (Secret Manager, no redeploy)
# ---------------------------------------------------------------------------
_kill_cache: dict = {"value": False, "at": 0.0}
_kill_lock = threading.Lock()


def _read_kill_secret() -> bool:
    """Read the kill-switch secret from Secret Manager via REST. True = halt.
    
    Uses the metadata server for auth (no client library needed).
    """
    import urllib.request
    import urllib.error
    
    project = os.environ.get("GCP_PROJECT") or "streetsmart-hermes-poc"
    try:
        # Get access token from metadata server
        token_req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(token_req, timeout=5) as resp:
            token_data = json.loads(resp.read().decode())
            access_token = token_data.get("access_token")
        if not access_token:
            return False
        
        # Read the secret
        secret_url = (
            f"https://secretmanager.googleapis.com/v1/"
            f"projects/{project}/secrets/{KILL_SWITCH_SECRET}/versions/latest:access"
        )
        secret_req = urllib.request.Request(
            secret_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(secret_req, timeout=5) as resp:
            secret_data = json.loads(resp.read().decode())
            import base64
            val = base64.b64decode(secret_data["payload"]["data"]).decode("utf-8").strip().lower()
            return val in ("1", "true", "yes", "on")
    except Exception as e:
        # Secret doesn't exist yet (normal before first use) or no access —
        logger.debug("kill-switch secret unreadable: %s", e)
        return False
        # treat as OFF, don't fail the dispatch path.
        logger.debug("kill-switch secret read failed (treated as OFF): %s", e)
        return False


def kill_switch_active() -> bool:
    """True if EITHER the env-var kill switch OR the Secret Manager secret
    says halt. The secret is cached for KILL_SWITCH_CACHE_S seconds."""
    # Env vars first (fast path, existing behavior).
    for var in ("ROBIE_HALT", "ROBIE_READ_ONLY"):
        if os.environ.get(var, "").strip().lower() in ("1", "true", "yes"):
            return True
    # Secret Manager (cached).
    now = time.time()
    with _kill_lock:
        if now - _kill_cache["at"] < KILL_SWITCH_CACHE_S:
            return _kill_cache["value"]
        value = _read_kill_secret()
        _kill_cache["value"] = value
        _kill_cache["at"] = now
        return value


def reset_kill_cache_for_tests():
    with _kill_lock:
        _kill_cache["value"] = False
        _kill_cache["at"] = 0.0
