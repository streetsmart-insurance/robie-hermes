"""Bland call port. The only dial path that uses bland_transport.

Dry-run and unit-test fakes never construct this with execute=True.
A kill switch, a non-E.164 number, or a transport refusal posts nothing.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, Optional

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
from .robie_call_handler import bland_payload_spec

logger = logging.getLogger(__name__)


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
    ):
        self.env = env
        self.hostname = hostname
        self.api_key = api_key
        self.secret_reader = secret_reader
        self.urlopen = urlopen
        self.execute = execute

    def _key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.secret_reader is None:
            raise BlandTransportRefused("Bland API key reader is not configured")
        return self.secret_reader(bland_api_key_secret(self.env))

    def place_call_with_double_dial(
        self,
        phone: str,
        task_text: str,
        first_sentence: str,
        voicemail_message: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if kill_switch_engaged(self.env, self.secret_reader):
            logger.warning("not dialing; bland-dispatcher-kill-switch is engaged")
            return {"success": False, "error": "kill switch engaged", "call_ids": []}
        text = str(phone or "").strip()
        dial = to_e164_us(text) if text.startswith("+") else None
        if not dial:
            logger.warning("refusing dial; value is not a validated E.164 phone")
            return {"success": False, "error": "refused non-E.164 phone", "call_ids": []}
        if self.execute is not True:
            return {"success": False, "error": "execute is off", "call_ids": []}
        cap = max_duration_minutes(self.env)
        body = bland_payload_spec(
            dial, task_text, first_sentence, voicemail_message, 1, metadata=metadata,
        )
        body["max_duration"] = int(cap) if cap == int(cap) else cap
        logger.info("placing Bland call to a number ending %s", dial[-4:])
        try:
            result = post_call(
                body,
                api_key=self._key(),
                execute=True,
                env=self.env,
                hostname=self.hostname,
                urlopen=self.urlopen,
            )
        except (BlandTransportRefused, BlandTransportError) as exc:
            return {"success": False, "error": str(exc)[:300], "call_ids": []}
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if not isinstance(call_id, str) or not call_id:
            return {"success": False, "error": "Bland accepted no call id", "call_ids": []}
        return {
            "success": True,
            "call_ids": [call_id],
            "attempts": [],
            "voicemail_hit": False,
            "redialed": False,
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
