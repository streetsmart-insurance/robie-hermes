"""Bland call port. The only dial path that uses bland_transport.

Dry-run and unit-test fakes never construct this with execute=True.
A kill switch, a non-E.164 number, or a transport refusal posts nothing.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .bland_double_dial import (
    PINNED_CALLER_ID,
    DoubleDialConfig,
    classify_outcome,
    decide_redial,
    redial_eligible,
)
from .bland_transport import (
    BlandTransportError,
    BlandTransportRefused,
    bland_api_key_secret,
    get_call,
    kill_switch_engaged,
    max_duration_minutes,
    post_call,
)
from .ezlynx_applicant_phone import to_e164_us
from .report_clock import in_calling_window
from .robie_call_handler import bland_payload_spec

# About 140 seconds of polls, inside the 180-second redial window.
# Real Bland voicemail calls run about 9–60 seconds plus ring time.
# The previous (0, 5, 10, 15) budget stopped near 30 seconds, so a
# voicemail that finished at 31 seconds never placed attempt 2.
# One intake run dials at most 25 calls, one after another.
# 25 * (140s poll + 10s redial wait + up to 60s outcome poll) is about
# 87.5 minutes. The intake unit's TimeoutStartSec is 120 minutes so
# that budget fits. The per-run cap stays 25.
DEFAULT_POLL_WAITS = (0, 10, 10, 10, 15, 15, 20, 20, 20, 20)

logger = logging.getLogger(__name__)

REAL_CLIENTS_ENV = "ROBIE_PHONE_REAL_CLIENTS"
JAKE_CELL_SECRET = "robie-test-jake-cell"


def _test_dial_only(
    env: Mapping[str, str] | None, hostname: str | None,
) -> bool:
    """Test never posts a client number, even if real-client dialing is on."""
    source = {} if env is None else env
    if str(source.get("ROBIE_ENV") or "").strip().upper() == "TEST":
        return True
    short = str(hostname or "").split(".")[0]
    return short == "hermes-test-01"


def select_dial_target(
    phone: str,
    *,
    env: Mapping[str, str] | None,
    secret_reader: Callable[[str], str] | None,
    hostname: str | None = None,
) -> tuple[str | None, str | None]:
    """The number to post, or an error.

    Real client numbers require ROBIE_PHONE_REAL_CLIENTS=1 and a host
    that is not Test. On Test (ROBIE_ENV=TEST or hermes-test-01) the only
    number that can be posted is the test cell from Secret Manager secret
    robie-test-jake-cell. The client phone is never used as a fallback.
    That value is never hardcoded and the full number is never logged.
    """
    source = {} if env is None else env
    if source.get(REAL_CLIENTS_ENV) == "1" and not _test_dial_only(source, hostname):
        text = str(phone or "").strip()
        dial = to_e164_us(text) if text.startswith("+") else None
        if not dial:
            return None, "real-client dial refused; value is not E.164"
        return dial, None
    if secret_reader is None:
        return None, "test mode has no Jake cell reader; not dialing"
    try:
        raw = str(secret_reader(JAKE_CELL_SECRET) or "").strip()
    except Exception:
        return None, "test mode could not read the Jake cell secret; not dialing"
    # The secret is stored as ten digits (7326688161) on Test. That is
    # unambiguous, so it is accepted alongside +1 E.164. Anything else
    # (letters, extensions, other lengths) still refuses.
    plain = raw.lstrip("+")
    target = to_e164_us(raw) if plain.isdigit() and len(plain) in (10, 11) else None
    if not target:
        return None, "test mode Jake cell secret is not E.164; not dialing"
    logger.info("test mode: dialing the configured test cell ending %s", target[-4:])
    return target, None


class BlandTransportCallPort:
    """BlandCallPort implemented on the existing transport."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        hostname: str | None = None,
        api_key: str = "",
        secret_reader: Callable[[str], str] | None = None,
        urlopen: Callable[..., Any] | None = None,
        execute: bool = False,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_waits: tuple[float, ...] | None = None,
    ):
        self.env = env
        self.hostname = hostname
        self.api_key = api_key
        self.secret_reader = secret_reader
        self.urlopen = urlopen
        self.execute = execute
        self.sleeper = sleeper or time.sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_waits = DEFAULT_POLL_WAITS if poll_waits is None else poll_waits

    def _key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.secret_reader is None:
            raise BlandTransportRefused("Bland API key reader is not configured")
        return self.secret_reader(bland_api_key_secret(self.env))

    def _halt_reason(self) -> str | None:
        source = os.environ if self.env is None else self.env
        if str(source.get("ROBIE_CALL_HALT") or "").strip() == "1":
            return "ROBIE_CALL_HALT"
        if self.secret_reader is None or kill_switch_engaged(self.env, self.secret_reader):
            if self.secret_reader is None:
                return "kill switch unreadable"
            return "kill switch engaged"
        return None

    def _read_call(self, call_id: str, api_key: str) -> dict[str, Any] | None:
        try:
            detail = get_call(
                call_id,
                api_key=api_key,
                execute=True,
                env=self.env,
                hostname=self.hostname,
                urlopen=self.urlopen,
            )
        except (BlandTransportRefused, BlandTransportError):
            return None
        return detail if isinstance(detail, dict) else None

    def _post_attempt(
        self,
        *,
        api_key: str,
        dial: str,
        task_text: str,
        first_sentence: str,
        voicemail_message: str,
        attempt: int,
        metadata: Optional[dict[str, Any]],
        transfer: str | None,
    ) -> tuple[str | None, str | None]:
        body = bland_payload_spec(
            dial, task_text, first_sentence, voicemail_message, attempt,
            metadata=metadata, transfer_phone_number=transfer,
        )
        cap = max_duration_minutes(self.env)
        body["max_duration"] = int(cap) if cap == int(cap) else cap
        try:
            result = post_call(
                body,
                api_key=api_key,
                execute=True,
                env=self.env,
                hostname=self.hostname,
                urlopen=self.urlopen,
            )
        except (BlandTransportRefused, BlandTransportError) as exc:
            return None, str(exc)[:300]
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if not isinstance(call_id, str) or not call_id:
            return None, "Bland accepted no call id"
        return call_id, None

    def place_call_with_double_dial(
        self,
        phone: str,
        task_text: str,
        first_sentence: str,
        voicemail_message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        halted = self._halt_reason()
        if halted:
            logger.warning("not dialing; %s", halted)
            return {"success": False, "error": halted, "call_ids": []}
        host = self.hostname if self.hostname else socket.gethostname()
        dial, target_error = select_dial_target(
            phone, env=self.env, secret_reader=self.secret_reader, hostname=host,
        )
        if target_error or not dial:
            logger.warning("not dialing: %s", target_error or "no dial target")
            return {"success": False, "error": target_error or "no dial target", "call_ids": []}
        if self.execute is not True:
            return {"success": False, "error": "execute is off", "call_ids": []}
        transfer = None
        if isinstance(metadata, dict):
            raw_transfer = str(metadata.get("transfer_phone_number") or "").strip()
            if raw_transfer.startswith("+"):
                transfer = raw_transfer
        try:
            api_key = self._key()
        except Exception:
            logger.warning("not dialing; Bland API key is unreadable")
            return {"success": False, "error": "Bland API key unreadable; not dialing", "call_ids": []}
        logger.info("placing Bland call to a number ending %s", dial[-4:])
        dispatch_at = self.clock()
        call_id, error = self._post_attempt(
            api_key=api_key, dial=dial, task_text=task_text,
            first_sentence=first_sentence, voicemail_message=voicemail_message,
            attempt=1, metadata=metadata, transfer=transfer,
        )
        if error or not call_id:
            return {"success": False, "error": error or "Bland accepted no call id", "call_ids": []}

        dial_config = DoubleDialConfig()
        classification = classify_outcome(None, config=dial_config)
        detail: dict[str, Any] | None = None
        for wait in self.poll_waits:
            if wait:
                self.sleeper(wait)
            detail = self._read_call(call_id, api_key)
            if detail is None:
                break
            classification = classify_outcome(detail, config=dial_config)
            if classification.conclusive:
                break

        single = {
            "success": True,
            "call_ids": [call_id],
            "attempts": [],
            "voicemail_hit": classification.outcome.value == "voicemail_no_message",
            "redialed": False,
            "recording_url": None,
            "error": None,
        }
        if not classification.conclusive or not redial_eligible(
            classification, config=dial_config,
        ):
            return single
        outcome_at = self.clock()
        # Jake's double dial: wait, then re-read every halt and the window.
        self.sleeper(dial_config.redial_delay_seconds)
        now = outcome_at + timedelta(seconds=dial_config.redial_delay_seconds)
        decision = decide_redial(
            attempt_outcomes=[classification.outcome],
            classification=classification,
            first_dispatch_at=dispatch_at,
            now=now,
            caller_id=PINNED_CALLER_ID,
            config=dial_config,
            first_outcome_at=outcome_at,
        )
        if not decision.redial:
            return single
        halted = self._halt_reason()
        if halted:
            logger.warning("not placing attempt 2; %s", halted)
            single["error"] = halted
            return single
        if not in_calling_window(self.clock()):
            logger.warning("not placing attempt 2; outside the calling window")
            single["error"] = "outside the calling window"
            return single
        second_id, second_error = self._post_attempt(
            api_key=api_key, dial=dial, task_text=task_text,
            first_sentence=first_sentence, voicemail_message=voicemail_message,
            attempt=2, metadata=metadata, transfer=transfer,
        )
        if second_error or not second_id:
            single["error"] = second_error or "attempt 2 accepted no call id"
            return single
        return {
            "success": True,
            "call_ids": [call_id, second_id],
            "attempts": [],
            "voicemail_hit": True,
            "redialed": True,
            "recording_url": None,
            "error": None,
        }

    def recent_calls(self, phone: str, since_seconds: int = 1800) -> dict[str, Any]:
        del phone, since_seconds
        return {"ok": False, "reason": "Bland history is not wired on this port"}

    def get_call_status(self, call_id: str) -> dict[str, Any]:
        if kill_switch_engaged(self.env, self.secret_reader) or self.execute is not True:
            return {"ok": False, "status": "unknown"}
        try:
            detail = get_call(
                call_id,
                api_key=self._key(),
                execute=True,
                env=self.env,
                hostname=self.hostname,
                urlopen=self.urlopen,
            )
        except (BlandTransportRefused, BlandTransportError):
            return {"ok": False, "status": "unknown"}
        return {
            "ok": True,
            "status": detail.get("status") or detail.get("queue_status") or "unknown",
            "answered_by": detail.get("answered_by"),
            "duration_s": detail.get("call_length") or detail.get("duration"),
            "ended_at": detail.get("end_at") or detail.get("ended_at"),
        }
