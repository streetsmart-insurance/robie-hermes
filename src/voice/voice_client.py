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
from src.voice.call_directory import normalize_phone_e164
from src.voice.context_hydrator import (
    CALL_TYPE_CLIENT_FOLLOWUP,
    CALL_TYPE_CLIENT_OUTREACH,
    CallingDossier,
    client_account_context_name,
    client_spoken_greeting,
    is_client_call_type,
    spoken_client_first_name,
)
from src.voice.outreach_pathways import (
    PATHWAY_GENERIC,
    assigned_producer_first_name,
    build_outreach_live_script,
    build_outreach_voicemail_script,
    extract_action_date,
    infer_outreach_pathway,
)

logger = logging.getLogger("carrier_voice_client")

# StreetSmart RingCentral office PBX. Carlo decision 2026-09-05: client_followup
# and client_outreach voicemail / busy / no close uses the agency main, not a
# producer cell or DID. Never invent Jake's DID.
AGENCY_MAIN_CALLBACK_DISPLAY = "732-462-8343"
AGENCY_MAIN_CALLBACK_E164 = "+17324628343"
AGENCY_MAIN_CALLBACK_SPOKEN = "seven three two, four six two, eight three four three"

# Bland send-call fires the transfer action as soon as the model says
# "transfer" / "transferring" (https://docs.bland.ai/api-v1/post/calls).
# Speak a complete client-facing handoff line that avoids those trigger
# words so the callee hears the whole sentence, then fire the action
# promptly. Do not instruct a timed silent hold.
WARM_TRANSFER_CLIENT_HANDOFF_LINE = "Please stay on the line while I connect you."


def warm_transfer_timing_rules() -> str:
    """Prompt rule: finish the spoken handoff sentence, then transfer promptly."""
    return (
        "WARM TRANSFER TIMING (mandatory on every warm transfer): "
        f'First speak this complete sentence to the person on this call: '
        f'"{WARM_TRANSFER_CLIENT_HANDOFF_LINE}" '
        "Finish every word of that sentence before using the transfer action. "
        "Do not cut yourself off mid-sentence. Do not say the words \"transfer\" "
        "or \"transferring\" in the handoff sentence — those words fire the Bland "
        "transfer action immediately and the callee barely hears the line. "
        "After the sentence is fully spoken, use the transfer action promptly "
        '(say "transfer"). Never fire the transfer action while still speaking. '
        "Do not insert a silent hold or timed wait before transferring."
    )


def _warm_transfer_action_clause(destination: str) -> str:
    """Call-objective line: finish the spoken handoff, then transfer promptly."""
    return (
        f"speak the full handoff line (\"{WARM_TRANSFER_CLIENT_HANDOFF_LINE}\"), "
        f"finish every word of that sentence, then transfer them to {destination} "
        "using the transfer action"
    )


def client_followup_callback_close() -> str:
    """Spoken close / voicemail ask-back for insured-facing calls (agency main)."""
    return (
        f"Please call StreetSmart back at {AGENCY_MAIN_CALLBACK_DISPLAY} "
        f'(say it naturally: "{AGENCY_MAIN_CALLBACK_SPOKEN}").'
    )


def agency_main_callback_close() -> str:
    """Alias used by client_outreach (same agency main as lead follow-up)."""
    return client_followup_callback_close()


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
        call_type = getattr(dossier, "call_type", None)
        if call_type == CALL_TYPE_CLIENT_OUTREACH:
            return self._build_client_outreach_prompt(dossier, custom_instructions)
        if call_type == CALL_TYPE_CLIENT_FOLLOWUP:
            return self._build_client_followup_prompt(dossier, custom_instructions)
        return self._build_carrier_prompt(dossier, custom_instructions)

    def _requestor_display(self, dossier: CallingDossier) -> str:
        return dossier.requestor_name or "the requestor"

    def _producer_display(self, dossier: CallingDossier, fallback: str = "your producer") -> str:
        return dossier.producer_name or fallback

    def _assigned_producer_display(self, dossier: CallingDossier) -> str:
        return dossier.assigned_producer_name or "the assigned producer"

    def _assigned_producer_first(self, dossier: CallingDossier) -> Optional[str]:
        return assigned_producer_first_name(dossier.assigned_producer_name)

    def _outreach_pathway(self, dossier: CallingDossier) -> str:
        return getattr(dossier, "outreach_pathway", None) or infer_outreach_pathway(
            dossier.custom_instructions
        ) or PATHWAY_GENERIC

    def _outreach_transfer_phone(self, dossier: CallingDossier) -> Optional[str]:
        """Assigned Producer DID only. Never requestor. Never Sales Center."""
        return normalize_phone_e164(getattr(dossier, "assigned_producer_phone", None))

    def _followup_transfer_phone(self, dossier: CallingDossier) -> Optional[str]:
        return normalize_phone_e164(dossier.requestor_phone)

    def _client_briefing_identity(self, dossier: CallingDossier) -> str:
        """First name only toward the client; business name is account context."""
        first = spoken_client_first_name(dossier.client_first_name)
        return first or "the client"

    def build_transfer_briefing(self, dossier: CallingDossier) -> str:
        """Short briefing Bland/Robie should give the requestor before merging.

        Spoken only to the destination (requestor / Assigned Producer) after
        they answer the proxy call — never to the client. ``Connecting you now``
        here is staff-side merge language, not the client handoff line.

        Client paths never say a full personal name (``Buster (Buster Brown)``).
        Business named-insured may appear as account context
        (``I have Buster on the line about Green Lion Lawn Care``).
        """
        requestor = self._requestor_display(dossier)
        producer = self._producer_display(dossier, fallback="the producer")
        if is_client_call_type(getattr(dossier, "call_type", None)):
            person = self._client_briefing_identity(dossier)
            account = client_account_context_name(
                dossier.insured_name, dossier.client_first_name
            )
            if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_OUTREACH:
                producer = self._assigned_producer_display(dossier)
                about = f"about {account}, policy" if account else "about their policy"
                return (
                    f"Hi {producer}, this is Robie from StreetSmart. I have {person} "
                    f"on the line {about} "
                    f"{dossier.policy_number}. Connecting you now."
                )
            about = (
                f"about {account} — the quote"
                if account
                else "about the quote"
            )
            return (
                f"Hi {requestor}, this is Robie from StreetSmart. I have {person} "
                f"on the line {about} {producer} "
                f"put together for policy {dossier.policy_number}. Connecting you now."
            )
        return (
            f"Hi {requestor}, this is Robie from StreetSmart. I have "
            f"{dossier.carrier_name} on the line regarding {dossier.insured_name}, "
            f"policy {dossier.policy_number}. Connecting you now."
        )

    def build_client_first_sentence(self, dossier: CallingDossier) -> str:
        if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_OUTREACH:
            return build_outreach_live_script(
                pathway=self._outreach_pathway(dossier),
                client_first=spoken_client_first_name(dossier.client_first_name),
                line_of_business=dossier.line_of_business,
                carrier_name=dossier.carrier_name if dossier.carrier_name != dossier.insured_name else None,
                producer_first=self._assigned_producer_first(dossier),
                action_date=extract_action_date(
                    dossier.custom_instructions, dossier.expiration_date
                ),
                csr_instructions=dossier.custom_instructions,
            )
        greeting = client_spoken_greeting(dossier.client_first_name)
        producer = dossier.producer_name or "your producer"
        return (
            f"{greeting}, this is Robie from StreetSmart — I'm calling about the quote "
            f"{producer} put together for you. Are you free to discuss it?"
        )

    def _custom_instructions_clause(
        self, dossier: CallingDossier, custom_instructions: Optional[str]
    ) -> str:
        ci = custom_instructions or dossier.custom_instructions
        if ci:
            return f"\nSpecific CSR instructions to convey: {ci}"
        return ""

    def _transfer_objective_block(self, dossier: CallingDossier) -> str:
        if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_OUTREACH:
            return self._outreach_transfer_objective_block(dossier)
        requestor = self._requestor_display(dossier)
        if not dossier.requestor_phone or not dossier.requestor_name:
            no_xfer = (
                "\nTRANSFER: Do not transfer this call. The person who requested this "
                "Robie Call has no phone on file in the voice directory. Do not guess "
                "another producer or fall back to the EZLynx Producer field."
            )
            if is_client_call_type(getattr(dossier, "call_type", None)):
                no_xfer += (
                    f" {client_followup_callback_close()} Do not leave a producer "
                    "personal or DID number unless it is explicitly written in the CSR instructions."
                )
            return no_xfer
        briefing = self.build_transfer_briefing(dossier)
        timing = warm_transfer_timing_rules()
        if is_client_call_type(getattr(dossier, "call_type", None)):
            callback = client_followup_callback_close()
            return f"""
WARM TRANSFER TO REQUESTOR:
- Destination: {requestor} ({dossier.requestor_phone}) — the person who invoked Robie Call.
- Only transfer if they clearly agree to speak with {requestor} now.
- If they say no, are busy, or you reach voicemail, give a short polite close and do not transfer.
- {callback} Do not leave a producer personal or DID number unless it is explicitly written in the CSR instructions.
- Never transfer to the EZLynx Producer unless that person is also the requestor.
- {timing}
- After the handoff sentence is fully spoken, use the transfer action promptly (say "transfer") and brief {requestor}:
  "{briefing}"
- The briefing above is spoken only to {requestor} after they answer — never to the person already on this call.
"""
        return f"""
WARM TRANSFER TO REQUESTOR:
- Destination: {requestor} ({dossier.requestor_phone}) — the person who invoked Robie Call.
- After a live human at the carrier is confirmed as the right desk, offer to connect them with {requestor}, or transfer immediately if they ask for the person who requested this call.
- Do not transfer until you have confirmed you reached the correct desk (or they asked for the requestor).
- Never transfer to the EZLynx Producer unless that person is also the requestor.
- {timing}
- After the handoff sentence is fully spoken, use the transfer action promptly (say "transfer") and brief {requestor}:
  "{briefing}"
- The briefing above is spoken only to {requestor} after they answer — never to the person already on this call.
"""

    def _outreach_transfer_objective_block(self, dossier: CallingDossier) -> str:
        producer = self._assigned_producer_display(dossier)
        producer_first = self._assigned_producer_first(dossier) or producer
        phone = self._outreach_transfer_phone(dossier)
        callback = client_followup_callback_close()
        if not phone or not dossier.assigned_producer_name:
            return (
                "\nTRANSFER: Do not transfer this call. The account Assigned Producer "
                "has no E.164 DID in the voice directory. Do not guess another "
                "producer, do not transfer to the label invoker / requestor, and do "
                f"not fall back to the Sales Center producer. {callback} Do not leave "
                "a producer personal or DID number unless it is explicitly written "
                "in the CSR instructions."
            )
        briefing = self.build_transfer_briefing(dossier)
        timing = warm_transfer_timing_rules()
        return f"""
WARM TRANSFER TO ASSIGNED PRODUCER:
- Destination: {producer} ({phone}) — account Assigned Producer (GetApplicantSidebar Assignment.AssignedTo), not the label invoker and not Sales Center producerName.
- Only transfer if they clearly agree to speak with {producer_first} now.
- If they say no, are busy, or you reach voicemail, give a short polite close and do not transfer.
- {callback} Do not leave a producer personal or DID number unless it is explicitly written in the CSR instructions.
- Never transfer to the label invoker / requestor. Never fall back to Sales Center producerName or another staff DID.
- {timing}
- After the handoff sentence is fully spoken, use the transfer action promptly (say "transfer") and brief {producer}:
  "{briefing}"
- The briefing above is spoken only to {producer} after they answer — never to the person already on this call.
"""

    def _build_client_followup_prompt(
        self, dossier: CallingDossier, custom_instructions: Optional[str] = None
    ) -> str:
        greeting = client_spoken_greeting(dossier.client_first_name)
        producer = self._producer_display(dossier)
        requestor = self._requestor_display(dossier)
        custom_instructions_clause = self._custom_instructions_clause(dossier, custom_instructions)
        transfer_block = self._transfer_objective_block(dossier)
        spoken_first = spoken_client_first_name(dossier.client_first_name) or "unknown"
        return f"""You are Robie, an autonomous operations specialist calling from StreetSmart Insurance.

CALL DETAILS:
- Call type: client follow-up
- Client first name: {spoken_first}
- Insured / account: {dossier.insured_name}
- Policy Number: {dossier.policy_number}
- Line of Business: {dossier.line_of_business}
- Producer (greeting only): {producer}
- Requestor / label invoker (warm transfer): {requestor}
- Requestor phone (warm transfer): {dossier.requestor_phone or 'not on file'}{custom_instructions_clause}

CALL OBJECTIVES:
1. Greet the client by first name only (never full name or LLC): "{greeting}, this is Robie from StreetSmart — I'm calling about the quote {producer} put together for you. Are you free to discuss it?"
2. If they clearly say yes / they are free to talk, {_warm_transfer_action_clause(requestor)}.
3. If they say no, are busy, or you reach voicemail, give a short polite close. Do not transfer. Leave a brief voicemail (or spoken close) asking them to call the agency back at {AGENCY_MAIN_CALLBACK_DISPLAY} (say it naturally: "{AGENCY_MAIN_CALLBACK_SPOKEN}"). Do not leave a producer personal or DID number unless it is explicitly written in the CSR instructions.
4. Never guess a different person. Only connect {requestor}. Do not fall back to the EZLynx Producer.
{transfer_block}
"""

    def _build_client_outreach_prompt(
        self, dossier: CallingDossier, custom_instructions: Optional[str] = None
    ) -> str:
        """Action-needed / cancellation outreach. Assigned Producer transfer."""
        spoken_first = spoken_client_first_name(dossier.client_first_name) or "unknown"
        producer = self._assigned_producer_display(dossier)
        producer_first = self._assigned_producer_first(dossier) or "your producer"
        pathway = self._outreach_pathway(dossier)
        live_script = self.build_client_first_sentence(dossier)
        custom_instructions_clause = self._custom_instructions_clause(dossier, custom_instructions)
        transfer_block = self._transfer_objective_block(dossier)
        transfer_phone = self._outreach_transfer_phone(dossier) or "not on file"
        return f"""You are Robie, an autonomous operations specialist calling from StreetSmart Insurance.

CALL DETAILS:
- Call type: client outreach
- Outreach pathway: {pathway}
- Client first name: {spoken_first}
- Insured / account: {dossier.insured_name}
- Policy Number: {dossier.policy_number}
- Line of Business: {dossier.line_of_business}
- Assigned Producer (warm transfer): {producer}
- Assigned Producer phone (warm transfer): {transfer_phone}
- Label invoker is NOT the transfer target.{custom_instructions_clause}

CALL OBJECTIVES:
1. Greet this person by first name only (never full name or LLC). Spoken script: "{live_script}"
2. Use the Splice-replacement conversational pathway ({pathway}). Do not mention a Sales Center producer or a quote greeting. Do not use press-1 / press-2 / IVR menus.
3. If they clearly say yes / they want to be connected, {_warm_transfer_action_clause(f"Assigned Producer {producer_first}")}.
4. If they say no, are busy, or you reach voicemail, give a short polite close. Do not transfer. Leave a brief voicemail (or spoken close) asking them to call the agency back at {AGENCY_MAIN_CALLBACK_DISPLAY} (say it naturally: "{AGENCY_MAIN_CALLBACK_SPOKEN}"). Do not leave a producer personal or DID number unless it is explicitly written in the CSR instructions.
5. Never guess a different person. Only connect the Assigned Producer. Do not transfer to the label invoker / requestor. Do not fall back to Sales Center producerName.
{transfer_block}
"""

    def _build_carrier_prompt(
        self, dossier: CallingDossier, custom_instructions: Optional[str] = None
    ) -> str:
        agency_code_clause = (
            f"Our agency producer code with your company is {dossier.agency_code}."
            if dossier.agency_code
            else "We are calling from StreetSmart Insurance."
        )

        ivr_clause = ""
        if dossier.ivr_instructions:
            ivr_clause = f"\nPhone menu / IVR guidance: {dossier.ivr_instructions}"

        custom_instructions_clause = self._custom_instructions_clause(dossier, custom_instructions)
        transfer_block = self._transfer_objective_block(dossier)

        return f"""You are Robie, an autonomous operations and renewal specialist calling from StreetSmart Insurance.

CALL DETAILS:
- Target Carrier: {dossier.carrier_name}
- Policy Number: {dossier.policy_number}
- Insured Legal Name: {dossier.insured_name}
- Line of Business: {dossier.line_of_business}
- Expiration Date: {dossier.expiration_date or 'Upcoming'}
- Requesting staff (label invoker): {dossier.requestor_name or 'StreetSmart requestor'}
- Producer (greeting / quote attribution only): {dossier.producer_name or 'StreetSmart producer'}
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
6. After the live human is confirmed as the right desk, offer to connect them with {dossier.requestor_name or 'the person who requested this call'}, or transfer if they ask for the requestor. When they agree, {_warm_transfer_action_clause(dossier.requestor_name or 'the person who requested this call')}.
7. Record the representative's first name, conclude the call politely, and wish them a great day.
{transfer_block}
"""

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
            simulated = {
                "success": True,
                "mode": "SIMULATION",
                "call_id": f"sim_call_{dossier.policy_number.replace(' ', '_')}_001",
                "status": "DISPATCHED_SIMULATED",
                "phone_number": dossier.carrier_phone,
                "carrier": dossier.carrier_name,
                "policy_number": dossier.policy_number,
                "insured_name": dossier.insured_name,
                "prompt": prompt,
                "call_type": getattr(dossier, "call_type", None),
                "producer_name": dossier.producer_name,
                "requestor_name": dossier.requestor_name,
                "requestor_phone": dossier.requestor_phone,
                "transfer_mode": dossier.transfer_mode,
                "client_first_name": dossier.client_first_name,
            }
            simulated.update(self.build_bland_transfer_fields(dossier))
            return simulated

        # Live Bland AI Integration
        if self.provider == "bland_ai":
            return self._dispatch_bland_ai(dossier, prompt, webhook_url)

        # Live Retell AI Integration
        return self._dispatch_retell(dossier, prompt, webhook_url)

    def build_bland_transfer_fields(self, dossier: CallingDossier) -> Dict[str, Any]:
        """Bland send-call transfer fields.

        ``client_outreach`` → Assigned Producer DID only.
        ``client_followup`` / ``carrier`` → label invoker (requestor) DID.
        """
        if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_OUTREACH:
            phone = self._outreach_transfer_phone(dossier)
            if not phone:
                return {}
            return {
                "transfer_phone_number": phone,
                "transfer_list": {
                    "default": phone,
                    "assigned_producer": phone,
                },
            }
        requestor_phone = self._followup_transfer_phone(dossier)
        if not requestor_phone:
            return {}
        return {
            "transfer_phone_number": requestor_phone,
            "transfer_list": {
                "default": requestor_phone,
                "requestor": requestor_phone,
            },
        }

    def _voicemail_message(self, dossier: CallingDossier) -> str:
        if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_OUTREACH:
            return build_outreach_voicemail_script(
                pathway=self._outreach_pathway(dossier),
                client_first=spoken_client_first_name(dossier.client_first_name),
                line_of_business=dossier.line_of_business,
                carrier_name=dossier.carrier_name if dossier.carrier_name != dossier.insured_name else None,
                action_date=extract_action_date(
                    dossier.custom_instructions, dossier.expiration_date
                ),
                csr_instructions=dossier.custom_instructions,
            )
        if getattr(dossier, "call_type", None) == CALL_TYPE_CLIENT_FOLLOWUP:
            producer = dossier.producer_name or "your producer"
            greeting = client_spoken_greeting(dossier.client_first_name, voicemail=True)
            return (
                f"{greeting}, this is Robie from StreetSmart Insurance calling about the quote "
                f"{producer} put together for you. Please call us back at "
                f"{AGENCY_MAIN_CALLBACK_DISPLAY} — that's {AGENCY_MAIN_CALLBACK_SPOKEN}. "
                "Thank you!"
            )
        return (
            f"Hello, this is Robie from StreetSmart Insurance calling regarding Policy #{dossier.policy_number} "
            f"for {dossier.insured_name}. Please email any updates or documentation to robie@streetsmart.insurance. "
            "Thank you and have a great day!"
        )

    def _first_sentence(self, dossier: CallingDossier) -> str:
        if is_client_call_type(getattr(dossier, "call_type", None)):
            return self.build_client_first_sentence(dossier)
        return (
            f"Hello! My name is Robie calling from StreetSmart Insurance regarding "
            f"policy number {dossier.policy_number}."
        )

    def _bland_metadata(self, dossier: CallingDossier) -> Dict[str, Any]:
        return {
            "policy_number": dossier.policy_number,
            "insured_name": dossier.insured_name,
            "carrier_name": dossier.carrier_name,
            "applicant_id": dossier.applicant_id,
            "assigned_csr_email": dossier.assigned_csr_email,
            "call_type": getattr(dossier, "call_type", None),
            "client_first_name": dossier.client_first_name,
            "producer_name": dossier.producer_name,
            "requestor_name": dossier.requestor_name,
            "requestor_phone": dossier.requestor_phone,
            "assigned_producer_name": getattr(dossier, "assigned_producer_name", None),
            "assigned_producer_phone": getattr(dossier, "assigned_producer_phone", None),
            "outreach_pathway": getattr(dossier, "outreach_pathway", None),
            "transfer_mode": dossier.transfer_mode,
        }

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
            "first_sentence": self._first_sentence(dossier),
            "voicemail_action": "leave_message",
            "voicemail_message": self._voicemail_message(dossier),
            "metadata": self._bland_metadata(dossier),
        }
        payload.update(self.build_bland_transfer_fields(dossier))
        if payload.get("transfer_phone_number"):
            payload["webhook_events"] = ["post_transfer_transcript"]
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

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"Authorization": self.api_key or "", "Content-Type": "application/json"}
        if self.encrypted_key:
            headers["encrypted_key"] = self.encrypted_key
        return headers

    def get_call(self, call_id: str) -> Dict[str, Any]:
        """Fetch a completed Bland call (recording_url, transcript, summary)."""
        if not call_id:
            return {}
        if not self.api_key:
            logger.info("Skipping Bland get_call for %s: no VOICE_AI_API_KEY configured", call_id)
            return {}
        url = f"https://api.bland.ai/v1/calls/{call_id}"
        try:
            resp = requests.get(url, headers=self._auth_headers(), timeout=20)
            data = resp.json() if resp.content else {}
            if resp.status_code != 200 or not isinstance(data, dict):
                logger.warning(
                    "Bland get_call %s returned HTTP %s: %s",
                    call_id,
                    resp.status_code,
                    data if isinstance(data, dict) else resp.text[:200],
                )
                return {}
            return data
        except Exception as exc:
            logger.error("Failed to fetch Bland call %s: %s", call_id, exc)
            return {}

    def download_recording(self, recording_url: str, dest_path) -> bool:
        """Download a Bland recording URL to dest_path. Returns True on success."""
        from pathlib import Path

        if not recording_url:
            return False
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = self.api_key
        if self.encrypted_key:
            headers["encrypted_key"] = self.encrypted_key
        try:
            resp = requests.get(recording_url, headers=headers, timeout=60, stream=True)
            if resp.status_code != 200:
                logger.warning(
                    "Recording download HTTP %s from %s", resp.status_code, recording_url
                )
                return False
            with dest.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
            if dest.stat().st_size <= 0:
                logger.warning("Recording download produced empty file: %s", dest)
                return False
            return True
        except Exception as exc:
            logger.error("Failed to download recording from %s: %s", recording_url, exc)
            return False
