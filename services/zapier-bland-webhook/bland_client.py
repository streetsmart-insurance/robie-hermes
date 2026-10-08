"""Bland AI voice-call client with Jake's double-dial voicemail policy.

Jake's requirements (from 5 live test calls, 2026-10-02):
- Voice: Karen (voice id 29158307-9893-4149-8a75-bc9ce313d64e)
- AI name: Eva. Identify UP FRONT as an AI assistant calling on behalf of
  Jake from StreetSmart Insurance; state the reason in one sentence.
- Screener handling: answer directly and slowly, repeat if asked, stay on
  the line until connected to the target person.
- End the call ONLY for: person unavailable, or voicemail reached.
- Double-dial: 1st voicemail -> silent hangup, retry after 10 seconds;
  2nd voicemail -> full SLOW message with AI disclosure + callback number
  stated digit by digit.
- Callback number: 732-462-8343. Outbound caller ID: +17322986745.

Bland API notes:
- POST /v1/calls with phone_number, task, voice, record, from, etc.
- voicemail_action: "hangup" (silent) or "leave_message".
- voicemail_message: spoken only when voicemail is detected on that call.
- answered_by_enabled: True -> GET /v1/calls/{id} returns answered_by
  ("human" | "voicemail" | "unknown").
- Cloudflare sits in front of api.bland.ai: every request MUST carry a
  browser-like User-Agent or it 403s before Bland sees the key.
"""
import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

from config import Config

logger = logging.getLogger("bland_dispatcher.bland")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TERMINAL_STATUSES = {"completed", "ended", "failed", "canceled", "no-answer", "busy"}

_TRANSIENT_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)

# Circuit breaker: after this many consecutive place_call failures, stop
# hitting Bland for CIRCUIT_COOLDOWN_S (prevents hammering a down API).
CIRCUIT_THRESHOLD = 5
CIRCUIT_COOLDOWN_S = 120.0


def digit_by_digit(number: str) -> str:
    """'732-462-8343' -> '7 3 2, 4 6 2, 8 3 4 3' for slow TTS delivery."""
    digits = [c for c in number if c.isdigit()]
    return " ".join(digits)


class BlandClient:
    # Circuit-breaker state is class-level so it survives across the
    # per-dispatch BlandClient instances (each dispatch() builds its own).
    # Per worker process; good enough to stop a thundering herd.
    _cb_lock = threading.Lock()
    _cb_failures = 0
    _cb_opened_at = 0.0

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else Config.BLAND_API_KEY
        self.base = Config.BLAND_BASE.rstrip("/")

    @classmethod
    def _reset_circuit_for_tests(cls):
        with cls._cb_lock:
            cls._cb_failures = 0
            cls._cb_opened_at = 0.0

    def _circuit_open(self) -> bool:
        with BlandClient._cb_lock:
            if BlandClient._cb_failures < CIRCUIT_THRESHOLD:
                return False
            if time.time() - BlandClient._cb_opened_at > CIRCUIT_COOLDOWN_S:
                # Half-open: allow one probe through.
                BlandClient._cb_failures = 0
                return False
            return True

    def _circuit_record(self, success: bool):
        tripped = False
        with BlandClient._cb_lock:
            if success:
                BlandClient._cb_failures = 0
            else:
                was_open = BlandClient._cb_failures >= CIRCUIT_THRESHOLD
                BlandClient._cb_failures += 1
                if BlandClient._cb_failures >= CIRCUIT_THRESHOLD and not was_open:
                    BlandClient._cb_opened_at = time.time()
                    logger.error(
                        "Bland circuit breaker OPEN after %d consecutive failures",
                        BlandClient._cb_failures)
                    tripped = True
        if tripped:
            # Alert once per trip (not per failure while open).
            try:
                from alerts import post_chat_alert
                post_chat_alert(
                    "Bland API circuit breaker tripped — calls paused for 120s.")
            except Exception:
                logger.exception("circuit-breaker alert failed")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
        }

    # ------------------------------------------------------------------
    # Low-level API
    # ------------------------------------------------------------------
    def place_call(
        self,
        phone_number: str,
        task: str,
        first_sentence: str,
        voicemail_action: str = "hangup",
        voicemail_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/calls with retries and a circuit breaker.

        Returns the raw Bland response dict: {"success": True, "call_id": ...}
        or {"success": False, "error": ...}.

        Retry policy: timeouts, connection errors, HTTP 429 and 5xx are
        retried up to 3 times with exponential backoff. Other 4xx fail fast
        (except the legacy 'from'-number fallback, kept as a single retry).
        """
        if self._circuit_open():
            logger.error("Bland circuit breaker open; not placing call")
            return {"success": False, "error": "circuit breaker open (Bland unavailable)"}

        phone = self._normalize_phone(phone_number)
        payload: Dict[str, Any] = {
            "phone_number": phone,
            "task": task,
            "voice": Config.VOICE_ID,
            "model": "enhanced",
            "record": True,
            "answered_by_enabled": True,
            "wait_for_greeting": True,
            "first_sentence": first_sentence,
            "voicemail_action": voicemail_action,
            "from": Config.FROM_NUMBER,
            "transfer_phone_number": Config.TRANSFER_NUMBER,
        }
        if voicemail_message:
            payload["voicemail_message"] = voicemail_message
        if metadata:
            payload["metadata"] = metadata

        url = f"{self.base}/v1/calls"
        logger.info("POST %s phone=%s voicemail_action=%s", url, phone, voicemail_action)
        result = self._post_with_retries(url, payload)
        self._circuit_record(result.get("success", False))
        return result

    def _post_with_retries(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST with transient-failure retries. Single attempt semantics for
        the legacy 'from'-number fallback are preserved inside."""
        last_error = "unknown error"
        for i in range(3):
            try:
                resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
            except _TRANSIENT_EXC as e:
                last_error = f"network: {e}"
                logger.warning("Bland POST transient (%s); retry %d/3", e, i + 1)
                time.sleep(min(2 ** i, 8))
                continue
            except Exception as e:  # non-transient request failure
                return {"success": False, "error": f"network: {e}"}

            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}

            # If the from-number is rejected, retry once with Bland's default pool.
            if resp.status_code in (400, 422) and "from" in payload:
                logger.warning("Bland rejected 'from' number; retrying without it.")
                payload.pop("from", None)
                try:
                    resp = requests.post(url, json=payload, headers=self._headers(), timeout=30)
                    data = resp.json()
                except Exception as e:
                    return {"success": False, "error": f"network on retry: {e}"}

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_error = data.get("message", f"HTTP {resp.status_code}")
                logger.warning("Bland POST HTTP %s; retry %d/3",
                               resp.status_code, i + 1)
                time.sleep(min(2 ** i, 8))
                continue

            if resp.status_code in (200, 201) and data.get("call_id"):
                return {"success": True, "call_id": data.get("call_id"), "raw": data}
            return {
                "success": False,
                "error": data.get("message", f"HTTP {resp.status_code}"),
                "raw": data,
            }
        return {"success": False, "error": f"after retries: {last_error}"}

    def is_available(self) -> bool:
        """False when the circuit breaker is open (Bland recently failing).

        Dispatchers should check this BEFORE doing EZLynx lookups so a
        down Bland fails fast with a clear error instead of burning API
        calls and producing a confusing downstream failure.
        """
        return not self._circuit_open()

    def recent_calls(self, phone_number: str,
                     since_seconds: int = 300) -> Dict[str, Any]:
        """List Bland calls to this phone number within the window.

        Used after a POST timeout/network failure: the call may have been
        accepted by Bland even though we never got the response. If a
        recent call exists, the dispatcher reports 'unknown' instead of a
        false failure (and the freeform handler skips redialing).

        Returns {"ok": True, "calls": [...]} or {"ok": False, "error": ...}.
        Never raises.
        """
        url = f"{self.base}/v1/calls"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=20)
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            data = resp.json()
        except Exception as e:
            return {"ok": False, "error": f"network: {e}"}
        calls = data.get("calls") if isinstance(data, dict) else data
        if not isinstance(calls, list):
            return {"ok": False, "error": "unexpected response shape"}
        want_digits = "".join(c for c in str(phone_number) if c.isdigit())
        cutoff = time.time() - since_seconds
        matches = []
        for c in calls:
            if not isinstance(c, dict):
                continue
            c_digits = "".join(ch for ch in str(c.get("phone_number") or "") if ch.isdigit())
            # Match on trailing digits (E.164 vs local formatting).
            if not (c_digits and want_digits and
                    (c_digits.endswith(want_digits[-10:]) or
                     want_digits.endswith(c_digits[-10:]))):
                continue
            created = c.get("created_at") or c.get("createdAt") or 0
            try:
                # Bland returns ISO strings; best-effort parse.
                if isinstance(created, str):
                    from datetime import datetime, timezone
                    ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00")).timestamp()
                else:
                    ts = float(created)
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                matches.append(c)
        return {"ok": True, "calls": matches}

    def get_call(self, call_id: str) -> Dict[str, Any]:
        """GET /v1/calls/{call_id} -> status, answered_by, recording_url, etc."""
        url = f"{self.base}/v1/calls/{call_id}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            return resp.json() if resp.status_code == 200 else {"error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"error": f"network: {e}"}

    # ------------------------------------------------------------------
    # Double-dial orchestration (Jake's policy)
    # ------------------------------------------------------------------
    def call_with_double_dial(
        self,
        phone_number: str,
        task: str,
        first_sentence: str,
        voicemail_message: str,
        metadata: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
        halt_check: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """Place the call, applying Jake's double-dial voicemail policy.

        Attempt 1: voicemail_action=hangup (silent hangup on voicemail).
        If voicemail was hit: wait REDIAL_DELAY_SECONDS, then attempt 2 with
        voicemail_action=leave_message + the full slow voicemail_message.

        halt_check: optional zero-arg callable returning True when the run
        must stop (e.g. the kill switch was flipped mid-dispatch). Checked
        after the redial wait and before placing attempt 2 — a kill switch
        flipped during the first call must prevent the second dial.

        In dry_run mode nothing is dialed; the would-be payloads are logged
        and returned with mode="DRY_RUN".
        """
        result: Dict[str, Any] = {
            "attempts": [],
            "voicemail_hit": False,
            "redialed": False,
        }

        if dry_run:
            for attempt in (1, 2):
                payload = self._attempt_payload(
                    phone_number, task, first_sentence, voicemail_message,
                    attempt, metadata,
                )
                logger.info("[DRY_RUN] would place call (attempt %d): %s", attempt, phone_number)
                result["attempts"].append(
                    {"attempt": attempt, "mode": "DRY_RUN", "payload": payload}
                )
            result["mode"] = "DRY_RUN"
            return result

        # ---- Attempt 1: silent hangup on voicemail ----
        first = self.place_call(
            phone_number, task, first_sentence,
            voicemail_action="hangup", metadata=metadata,
        )
        result["attempts"].append({"attempt": 1, **first})
        if not first.get("success"):
            result["mode"] = "LIVE_BLAND_AI"
            return result

        call_id = first["call_id"]
        final = self._wait_for_terminal(call_id)
        answered_by = (final.get("answered_by") or "").lower()
        result["attempts"][0]["final_status"] = final

        if answered_by != "voicemail":
            result["mode"] = "LIVE_BLAND_AI"
            return result

        # ---- Voicemail hit: silent hangup happened. Redial after delay. ----
        result["voicemail_hit"] = True
        delay = Config.REDIAL_DELAY_SECONDS
        logger.info("Voicemail on attempt 1; redialing in %ds", delay)
        time.sleep(delay)

        # Kill switch may have been flipped during attempt 1 or the wait.
        # Never place the second dial after a halt.
        if halt_check is not None:
            try:
                if halt_check():
                    logger.warning("halt requested before redial; skipping attempt 2")
                    result["halted_before_redial"] = True
                    result["mode"] = "LIVE_BLAND_AI"
                    return result
            except Exception:
                logger.exception("halt_check raised; proceeding with redial")

        second = self.place_call(
            phone_number, task, first_sentence,
            voicemail_action="leave_message",
            voicemail_message=voicemail_message,
            metadata=metadata,
        )
        result["attempts"].append({"attempt": 2, **second})
        result["redialed"] = True
        result["mode"] = "LIVE_BLAND_AI"
        if second.get("success"):
            result["attempts"][1]["final_status"] = self._wait_for_terminal(second["call_id"])
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _attempt_payload(
        self, phone_number, task, first_sentence, voicemail_message, attempt, metadata
    ) -> Dict[str, Any]:
        """Build the exact Bland payload for an attempt (used by dry-run + tests)."""
        payload: Dict[str, Any] = {
            "phone_number": self._normalize_phone(phone_number),
            "task": task,
            "voice": Config.VOICE_ID,
            "model": "enhanced",
            "record": True,
            "answered_by_enabled": True,
            "wait_for_greeting": True,
            "first_sentence": first_sentence,
            "from": Config.FROM_NUMBER,
            "transfer_phone_number": Config.TRANSFER_NUMBER,
        }
        if attempt == 1:
            payload["voicemail_action"] = "hangup"  # silent hangup
        else:
            payload["voicemail_action"] = "leave_message"
            payload["voicemail_message"] = voicemail_message
        if metadata:
            payload["metadata"] = metadata
        return payload

    def _wait_for_terminal(self, call_id: str) -> Dict[str, Any]:
        """Poll until the call reaches a terminal status or we time out."""
        deadline = time.time() + Config.CALL_POLL_TIMEOUT_SECONDS
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_call(call_id)
            status = str(last.get("status", "")).lower()
            if status in TERMINAL_STATUSES:
                return last
            time.sleep(Config.CALL_POLL_INTERVAL_SECONDS)
        last["poll_timed_out"] = True
        return last

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        digits = "".join(c for c in str(phone) if c.isdigit())
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        if str(phone).strip().startswith("+"):
            return str(phone).strip()
        return f"+1{digits}"
