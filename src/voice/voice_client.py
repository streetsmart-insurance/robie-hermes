"""
Autonomous Voice AI Client for Carrier Phone Calls.
Supports Bland AI, Retell AI, and Mock/Dry-Run modes.
"""

import json
import logging
import os
import requests
from typing import Optional, Dict, Any

from src.config import settings
from src.voice.context_hydrator import CallingDossier

logger = logging.getLogger("carrier_voice_client")


class CarrierVoiceClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        provider: str = "bland_ai",  # 'bland_ai' or 'retell'
        from_phone_number: Optional[str] = None,
        encrypted_key: Optional[str] = None,
    ):
        self.provider = provider.lower()
        self.api_key = api_key or os.getenv("VOICE_AI_API_KEY") or getattr(settings, "voice_ai_api_key", None) or self._fetch_secret_key()
        self.from_phone = from_phone_number or os.getenv("VOICE_CALLER_ID") or getattr(settings, "voice_caller_id", "+17322986745")
        self.encrypted_key = (
            encrypted_key
            or os.getenv("VOICE_ENCRYPTED_KEY")
            or os.getenv("BLAND_ENCRYPTED_KEY")
            or getattr(settings, "voice_encrypted_key", None)
            or (self._fetch_encrypted_key() if not self.api_key else None)
        )

    @staticmethod
    def _fetch_secret_key() -> Optional[str]:
        try:
            from src.security.secrets_manager import SecretsManager
            sm = SecretsManager()
            return sm.get_credential("carrier_voice", "api_key")
        except Exception:
            return None

    @staticmethod
    def _fetch_encrypted_key() -> Optional[str]:
        try:
            from src.security.secrets_manager import SecretsManager
            sm = SecretsManager()
            return sm.get_credential("carrier_voice", "encrypted_key")
        except Exception:
            return None

    def build_call_prompt(self, dossier: CallingDossier, custom_instructions: Optional[str] = None) -> str:
        """Constructs conversational instructions for the Voice AI model."""
        agency_code_clause = (
            f"Our agency producer code with your company is {dossier.agency_code}."
            if dossier.agency_code
            else "We are calling from StreetSmart Insurance."
        )

        ivr_clause = ""
        if dossier.ivr_instructions:
            ivr_clause = f"\nPhone menu / IVR guidance: {dossier.ivr_instructions}"

        ci = custom_instructions or dossier.custom_instructions
        custom_instructions_clause = ""
        if ci:
            custom_instructions_clause = f"\nSpecific CSR instructions to convey: {ci}"

        prompt = f"""You are Robie, an autonomous operations and renewal specialist calling from StreetSmart Insurance.

CALL DETAILS:
- Target Carrier: {dossier.carrier_name}
- Policy Number: {dossier.policy_number}
- Insured Legal Name: {dossier.insured_name}
- Line of Business: {dossier.line_of_business}
- Expiration Date: {dossier.expiration_date or 'Upcoming'}
- Agency Reference: {agency_code_clause}{ivr_clause}{custom_instructions_clause}

CALL OBJECTIVES:
1. When navigating automated phone menus (IVR), select options for Commercial Lines Underwriting, Policy Servicing, or Renewals.
2. If placed on hold with music or ringing, stay on the line patiently and DO NOT speak until a human representative greets you.
3. Once connected to a live representative:
   - Introduce yourself warmly: "Hello! My name is Robie calling from StreetSmart Insurance. {agency_code_clause}"
   - State the policy you are inquiring about: "I'm following up on the upcoming renewal for {dossier.insured_name}, policy number {dossier.policy_number}."
   - Inquire about the renewal terms: "Could you let me know if renewal terms or a quote have been released for this account yet?"
4. If a quote has been issued:
   - Ask for the quoted renewal premium.
   - Ask where the packet was delivered (e.g. agent portal or emailed).
5. If terms are not yet released:
   - Inquire what is needed to issue terms (e.g. loss runs, renewal application, payroll verification).
   - Ask for the underwriter's direct email or estimated completion date.
6. Record the representative's first name, conclude the call politely, and wish them a great day.
"""
        return prompt

    def dispatch_call(
        self,
        dossier: CallingDossier,
        webhook_url: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Initiates outbound phone call to carrier.
        If dry_run=True or no API key, executes in mock simulation mode.
        """
        if not dossier.carrier_phone:
            logger.error(f"Cannot dispatch call for {dossier.policy_number}: No carrier phone number resolved.")
            return {
                "success": False,
                "error": "NO_CARRIER_PHONE",
                "policy_number": dossier.policy_number,
            }

        prompt = self.build_call_prompt(dossier)

        # Mock / Simulation Mode
        if dry_run or not self.api_key:
            logger.info(
                f"[SIMULATION] Outbound carrier call triggered for {dossier.policy_number} "
                f"({dossier.carrier_name} at {dossier.carrier_phone})"
            )
            return {
                "success": True,
                "mode": "SIMULATION",
                "call_id": f"sim_call_{dossier.policy_number.replace(' ', '_')}_001",
                "status": "DISPATCHED_SIMULATED",
                "phone_number": dossier.carrier_phone,
                "carrier": dossier.carrier_name,
                "policy_number": dossier.policy_number,
                "insured_name": dossier.insured_name,
                "prompt": prompt,
            }

        # Live Bland AI Integration
        if self.provider == "bland_ai":
            return self._dispatch_bland_ai(dossier, prompt, webhook_url)

        # Live Retell AI Integration
        return self._dispatch_retell(dossier, prompt, webhook_url)

    def _dispatch_bland_ai(
        self, dossier: CallingDossier, prompt: str, webhook_url: Optional[str]
    ) -> Dict[str, Any]:
        url = "https://api.bland.ai/v1/calls"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        if self.encrypted_key:
            headers["encrypted_key"] = self.encrypted_key

        clean_phone = dossier.carrier_phone
        if not clean_phone.startswith("+"):
            clean_phone = f"+1{clean_phone.replace('-', '').replace(' ', '')}"

        payload: Dict[str, Any] = {
            "phone_number": clean_phone,
            "task": prompt,
            "voice": "nat",  # Natural professional voice
            "model": "enhanced",
            "record": True,
            "answered_by_enabled": True,
            "wait_for_greeting": True,
            "ivr_navigation": True,
            "first_sentence": f"Hello! My name is Robie calling from StreetSmart Insurance regarding policy number {dossier.policy_number}.",
            "voicemail_action": "leave_message",
            "voicemail_message": (
                f"Hello, this is Robie from StreetSmart Insurance calling regarding Policy #{dossier.policy_number} "
                f"for {dossier.insured_name}. Please email any updates or documentation to robie@streetsmart.insurance. "
                "Thank you and have a great day!"
            ),
            "metadata": {
                "policy_number": dossier.policy_number,
                "insured_name": dossier.insured_name,
                "carrier_name": dossier.carrier_name,
                "applicant_id": dossier.applicant_id,
                "assigned_csr_email": dossier.assigned_csr_email,
            },
        }
        if self.from_phone:
            payload["from"] = self.from_phone

        if webhook_url:
            payload["webhook"] = webhook_url

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            data = resp.json()
            # If from number failed due to ownership, retry once without 'from'
            if resp.status_code in [400, 422] and "from" in payload:
                logger.warning(f"Bland AI rejected 'from' number ({self.from_phone}). Retrying with default pool...")
                payload.pop("from", None)
                resp = requests.post(url, json=payload, headers=headers, timeout=15)
                data = resp.json()

            if resp.status_code in [200, 201]:
                return {
                    "success": True,
                    "mode": "LIVE_BLAND_AI",
                    "call_id": data.get("call_id"),
                    "status": "DISPATCHED",
                    "phone_number": clean_phone,
                    "carrier": dossier.carrier_name,
                    "policy_number": dossier.policy_number,
                }
            return {
                "success": False,
                "error": data.get("message", f"HTTP {resp.status_code}"),
                "details": data,
            }
        except Exception as e:
            logger.error(f"Failed to dispatch Bland AI call: {e}")
            return {"success": False, "error": str(e)}

    def _dispatch_retell(
        self, dossier: CallingDossier, prompt: str, webhook_url: Optional[str]
    ) -> Dict[str, Any]:
        url = "https://api.retellai.com/v2/create-phone-call"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        clean_phone = dossier.carrier_phone
        if not clean_phone.startswith("+"):
            clean_phone = f"+1{clean_phone.replace('-', '').replace(' ', '')}"

        payload: Dict[str, Any] = {
            "from_number": self.from_phone,
            "to_number": clean_phone,
            "override_agent_id": os.getenv("RETELL_AGENT_ID"),
            "retell_llm_dynamic_variables": {
                "prompt": prompt,
                "policy_number": dossier.policy_number,
                "insured_name": dossier.insured_name,
            },
            "metadata": {
                "policy_number": dossier.policy_number,
                "applicant_id": dossier.applicant_id,
                "csr_email": dossier.assigned_csr_email,
            },
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            data = resp.json()
            if resp.status_code in [200, 201]:
                return {
                    "success": True,
                    "mode": "LIVE_RETELL",
                    "call_id": data.get("call_id"),
                    "status": "DISPATCHED",
                    "phone_number": clean_phone,
                }
            return {"success": False, "error": data.get("message", f"HTTP {resp.status_code}")}
        except Exception as e:
            return {"success": False, "error": str(e)}
