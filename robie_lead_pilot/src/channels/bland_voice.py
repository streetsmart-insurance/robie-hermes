"""
Bland AI Telephony Integration for Robie Outbound Cadences.

Features:
- Caller ID: +1 (732) 298-6745 (Monmouth County, NJ)
- Enforces strict Allowlist gating during testing/pilots
- Automatic warm-transfer routing to Assigned Producer DID
- Voicemail detection & leave message
- Live and Dry-run simulation modes
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.models.cadence_models import ApplicantLead, CadenceType, Opportunity, QuoteSummary
from src.scripts.voice_scripts import VoiceScriptBuilder, VoiceScriptPackage

logger = logging.getLogger("bland_voice")

BLAND_API_URL = "https://api.bland.ai/v1/calls"
DEFAULT_CALLER_ID = "+17322986745"


class TelephonySafetyError(RuntimeError):
    """Raised when an unapproved number is targeted for live voice outreach."""
    pass


def normalize_phone_e164(phone: Optional[str]) -> Optional[str]:
    """Normalizes phone numbers to standard E.164 (+1XXXXXXXXXX)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    elif digits.startswith("+"):
        return phone.strip()
    return f"+{digits}" if digits else None


class BlandVoiceClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        allowlist_path: Optional[Path] = None,
        caller_id: str = DEFAULT_CALLER_ID,
    ):
        self.api_key = (
            api_key
            or os.getenv("VOICE_AI_API_KEY")
            or os.getenv("BLAND_API_KEY")
            or self._fetch_system_key()
            or ""
        )
        self.caller_id = caller_id
        self.allowlist_path = allowlist_path or Path("config/dial_allowlist.txt")
        self.allowlist: Set[str] = self._load_allowlist()

    @staticmethod
    def _fetch_system_key() -> Optional[str]:
        try:
            from src.security.secrets_manager import SecretsManager
            sm = SecretsManager()
            return sm.get_credential("carrier_voice", "api_key")
        except Exception:
            return None

    def _load_allowlist(self) -> Set[str]:
        allowed = set()
        if self.allowlist_path.exists():
            try:
                for line in self.allowlist_path.read_text(encoding="utf-8").splitlines():
                    cleaned = line.split("#")[0].strip()
                    if cleaned:
                        norm = normalize_phone_e164(cleaned)
                        if norm:
                            allowed.add(norm)
            except Exception as e:
                logger.warning("Could not read allowlist from %s: %s", self.allowlist_path, e)
        return allowed

    def is_number_allowed(self, phone: str) -> bool:
        norm = normalize_phone_e164(phone)
        return norm in self.allowlist if norm else False

    def build_payload(
        self,
        script_package: VoiceScriptPackage,
        recipient_phone: str,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        norm_phone = normalize_phone_e164(recipient_phone)
        payload: Dict[str, Any] = {
            "phone_number": norm_phone,
            "from": self.caller_id,
            "task": script_package.task_prompt,
            "first_sentence": script_package.first_sentence,
            "voice": "nat",
            "model": "enhanced",
            "wait_for_greeting": True,
            "answered_by_enabled": True,
            "ivr_navigation": True,
            "record": True,
            "voicemail_action": "leave_message",
            "voicemail_message": script_package.voicemail_message,
            "metadata": script_package.metadata,
        }

        # Warm transfer configuration
        if script_package.transfer_phone:
            norm_transfer = normalize_phone_e164(script_package.transfer_phone)
            if norm_transfer:
                payload["transfer_phone_number"] = norm_transfer
                payload["transfer_list"] = {
                    "default": norm_transfer,
                    "producer": norm_transfer,
                }
                payload["webhook_events"] = ["post_transfer_transcript"]

        if webhook_url:
            payload["webhook"] = webhook_url

        return payload

    def dispatch(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        cadence_type: CadenceType,
        touch_number: int,
        quote: Optional[QuoteSummary] = None,
        dry_run: bool = True,
        enforce_allowlist: bool = True,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Builds the voice payload and dispatches via Bland AI (or simulates in dry-run).
        """
        script_pkg = VoiceScriptBuilder.build_script(
            cadence_type=cadence_type,
            touch_number=touch_number,
            lead=lead,
            opportunity=opportunity,
            quote=quote,
            producer_transfer_did=lead.assigned_producer_phone,
        )

        norm_phone = normalize_phone_e164(lead.phone)
        if not norm_phone:
            return {
                "success": False,
                "error": "INVALID_PHONE_NUMBER",
                "phone": lead.phone,
            }

        # Allowlist Safety Gate
        if enforce_allowlist and not self.is_number_allowed(norm_phone):
            if not dry_run:
                raise TelephonySafetyError(
                    f"Safety Violation: Phone number {norm_phone} is not in {self.allowlist_path}. "
                    "Outbound live dial blocked."
                )
            logger.info("Dry-run allowlist note: %s is not in allowlist (simulation permitted)", norm_phone)

        payload = self.build_payload(script_pkg, norm_phone, webhook_url=webhook_url)

        if dry_run:
            call_id = f"sim_call_{lead.applicant_id}_T{touch_number}"
            logger.info("[DRY RUN] Simulated Bland call %s to %s ($0 spend)", call_id, norm_phone)
            return {
                "success": True,
                "mode": "DRY_RUN",
                "call_id": call_id,
                "phone": norm_phone,
                "payload": payload,
                "script_package": script_pkg,
            }

        # Live Bland AI Dispatch
        if not self.api_key:
            return {
                "success": False,
                "error": "MISSING_VOICE_API_KEY",
                "phone": norm_phone,
            }

        try:
            import requests

            headers = {
                "authorization": self.api_key,
                "content-type": "application/json",
            }
            resp = requests.post(BLAND_API_URL, json=payload, headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                call_id = data.get("call_id") or data.get("id")
                logger.info("Dispatched LIVE Bland call %s to %s", call_id, norm_phone)
                return {
                    "success": True,
                    "mode": "LIVE",
                    "call_id": call_id,
                    "phone": norm_phone,
                    "response": data,
                    "payload": payload,
                }
            else:
                logger.error("Bland API error %s: %s", resp.status_code, resp.text)
                return {
                    "success": False,
                    "mode": "LIVE",
                    "status_code": resp.status_code,
                    "error": resp.text,
                    "phone": norm_phone,
                }
        except Exception as exc:
            logger.error("Failed to post call to Bland AI: %s", exc)
            return {
                "success": False,
                "mode": "LIVE",
                "error": str(exc),
                "phone": norm_phone,
            }
