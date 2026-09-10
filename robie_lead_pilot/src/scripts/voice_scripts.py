"""
Bland AI Voice Prompt Generator & Conversational Logic for Robie.

Strictly enforces:
1. Grounding Invariant: Never claim a rate will change or quote will expire
   unless verified in EZLynx.
2. Agency Callback: Always StreetSmart main 732-462-8343, never a producer DID.
3. Live Warm Transfer: Only to assigned producer DID if prospect consents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

from src.models.cadence_models import ApplicantLead, CadenceType, Opportunity, QuoteSummary

logger = logging.getLogger("voice_scripts")

AGENCY_MAIN_CALLBACK_DISPLAY = "(732) 462-8343"
AGENCY_MAIN_CALLBACK_E164 = "+17324628343"
AGENCY_MAIN_CALLBACK_SPOKEN = "seven three two, four six two, eight three four three"
VOICE_CALLER_ID_E164 = "+17322986745"


@dataclass
class VoiceScriptPackage:
    first_sentence: str
    task_prompt: str
    voicemail_message: str
    transfer_phone: Optional[str]
    transfer_briefing: str
    metadata: Dict[str, Any]


class VoiceScriptBuilder:
    """Constructs compliant, high-conversion conversational payloads for Bland AI."""

    @classmethod
    def build_script(
        cls,
        cadence_type: CadenceType,
        touch_number: int,
        lead: ApplicantLead,
        opportunity: Opportunity,
        quote: Optional[QuoteSummary] = None,
        producer_transfer_did: Optional[str] = "+17324812520",
    ) -> VoiceScriptPackage:
        first_name = lead.spoken_first_name
        producer_name = lead.assigned_producer or "Jake Ferrara"
        producer_first = producer_name.split()[0]
        lob = opportunity.line_of_business or "insurance"

        # Rate / Expiration Grounding Gate
        verified_expiration_str = cls._format_verified_date(quote.verified_expiration_date if quote else None)
        verified_rate_str = cls._format_verified_date(quote.verified_rate_guarantee_date if quote else None)

        if cadence_type == CadenceType.INBOUND_LEAD:
            return cls._build_inbound_lead_script(
                touch_number=touch_number,
                first_name=first_name,
                producer_name=producer_name,
                producer_first=producer_first,
                lob=lob,
                transfer_did=producer_transfer_did,
                lead=lead,
                opportunity=opportunity,
            )
        elif cadence_type == CadenceType.QUOTED_PROSPECT:
            return cls._build_quoted_prospect_script(
                touch_number=touch_number,
                first_name=first_name,
                producer_name=producer_name,
                producer_first=producer_first,
                lob=lob,
                quote=quote,
                verified_expiration_str=verified_expiration_str,
                verified_rate_str=verified_rate_str,
                transfer_did=producer_transfer_did,
                lead=lead,
                opportunity=opportunity,
            )
        elif cadence_type == CadenceType.XDATE_OPPORTUNITY:
            return cls._build_xdate_script(
                touch_number=touch_number,
                first_name=first_name,
                producer_name=producer_name,
                producer_first=producer_first,
                lob=lob,
                opportunity=opportunity,
                transfer_did=producer_transfer_did,
                lead=lead,
            )
        else:
            raise ValueError(f"Unknown cadence type: {cadence_type}")

    @staticmethod
    def _format_verified_date(d: Optional[date]) -> Optional[str]:
        return d.strftime("%B %d") if d else None

    @classmethod
    def _build_inbound_lead_script(
        cls,
        touch_number: int,
        first_name: str,
        producer_name: str,
        producer_first: str,
        lob: str,
        transfer_did: Optional[str],
        lead: ApplicantLead,
        opportunity: Opportunity,
    ) -> VoiceScriptPackage:
        if touch_number == 1:
            first_sentence = (
                f"Hi {first_name}, this is Robie calling from StreetSmart Insurance on behalf of {producer_name}. "
                f"I saw you reached out for an insurance quote on your {lob} — do you have two minutes to connect "
                f"with {producer_first} to review your options?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance calling regarding your {lob} quote request. "
                f"Our producer {producer_name} is ready to help. Please give us a call back at {AGENCY_MAIN_CALLBACK_DISPLAY} "
                f"or reply to our email whenever you're free. Thank you!"
            )
        else:  # Touch 3 (Day 7 final follow-up)
            first_sentence = (
                f"Hi {first_name}, this is Robie following up from StreetSmart Insurance regarding your {lob} inquiry. "
                f"I wanted to see if you still needed our help before we close out your file?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance following up on your {lob} inquiry. "
                f"If you're still looking for coverage, give {producer_name} a call back at {AGENCY_MAIN_CALLBACK_DISPLAY}. "
                f"Otherwise, we will keep your file on hold. Have a wonderful day!"
            )

        task_prompt = f"""
You are Robie, an autonomous voice assistant for StreetSmart Insurance calling on behalf of licensed producer {producer_name}.
You are calling {first_name} regarding their recent inquiry for {lob} insurance.

CORE OBJECTIVE:
- Verify if {first_name} is available for a brief conversation.
- If they are available and interested, offer to warm-transfer them directly to {producer_first}.
- Never give coverage advice, legal recommendations, or bind coverage.

RULES OF ENGAGEMENT:
1. Warm Transfer: If the prospect says yes or wants details, say:
   "Great! Let me connect you directly with {producer_first} right now."
   Then invoke the transfer tool immediately to {transfer_did}.
2. If they are busy or in a rush:
   "No problem at all! When is a good time for {producer_first} to reach back out, or would you prefer an email?"
3. If they ask for human:
   "Of course! Let me get {producer_first} on the line right away."
4. If they want to bind coverage:
   "That's wonderful! Let me connect you directly with {producer_first} to finalize your application and bind coverage."
5. If they ask for coverage advice:
   "As an automated assistant, I can't recommend specific limits or advise on legal requirements, but {producer_first} is licensed and right here to advise you. Let me connect you."
6. Strict Urgency Ban: DO NOT state that a rate will increase or expire.
7. Callback Number: If asked for a callback number, cite {AGENCY_MAIN_CALLBACK_DISPLAY} (say: "{AGENCY_MAIN_CALLBACK_SPOKEN}").
8. Opt-Out / DNC: If the person says "stop calling", "remove me", "not interested", or "wrong number", apologize politely, end the call immediately, and do not argue.
"""
        briefing = f"Robie lead handoff: {first_name} inquiring about {lob} coverage. They agreed to speak with you."
        return VoiceScriptPackage(
            first_sentence=first_sentence,
            task_prompt=task_prompt.strip(),
            voicemail_message=vm,
            transfer_phone=transfer_did,
            transfer_briefing=briefing,
            metadata={
                "applicant_id": lead.applicant_id,
                "opportunity_id": opportunity.opportunity_id,
                "cadence_type": CadenceType.INBOUND_LEAD.value,
                "touch_number": touch_number,
                "producer_name": producer_name,
                "lob": lob,
            },
        )

    @classmethod
    def _build_quoted_prospect_script(
        cls,
        touch_number: int,
        first_name: str,
        producer_name: str,
        producer_first: str,
        lob: str,
        quote: Optional[QuoteSummary],
        verified_expiration_str: Optional[str],
        verified_rate_str: Optional[str],
        transfer_did: Optional[str],
        lead: ApplicantLead,
        opportunity: Opportunity,
    ) -> VoiceScriptPackage:
        carrier = quote.carrier_name if quote and quote.carrier_name else "our top carriers"
        premium_str = f"${quote.quoted_premium:,.2f}" if quote and quote.quoted_premium else None

        if touch_number == 1:
            first_sentence = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance following up on the {lob} proposal "
                f"that {producer_name} sent over. I wanted to see if you had a moment to review the numbers and ask any questions?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance following up on the {lob} quote prepared by {producer_name}. "
                f"Please give us a call back at {AGENCY_MAIN_CALLBACK_DISPLAY} or check your email to review. We're here to help!"
            )
        else:  # Touch 3 (Day 7)
            first_sentence = (
                f"Hi {first_name}, this is Robie checking in from StreetSmart Insurance regarding your {lob} quote. "
                f"Have you had a chance to decide on coverage, or did you want {producer_first} to look at any adjustments?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance regarding your {lob} quote with {producer_name}. "
                f"If you'd like to proceed or explore different deductible options, call us back at {AGENCY_MAIN_CALLBACK_DISPLAY}. Thank you!"
            )

        # Grounded expiration clause
        expiration_rule = "Do NOT mention quote expiration or rate increases unless explicitly asked."
        if verified_expiration_str:
            expiration_rule = f"The quote expiration date in our system is verified as {verified_expiration_str}. You may mention this date only if the customer asks how long the proposal is good for."

        task_prompt = f"""
You are Robie, an assistant calling from StreetSmart Insurance on behalf of {producer_name}.
You are calling {first_name} to follow up on a completed {lob} quote proposal ({carrier}).

GROUNDING RULE (STRICT):
{expiration_rule}
Never claim that rates are going up or that the quote is expiring today unless the customer asks and you cite verified data.

GOAL:
- Ask if they received the proposal and if the coverage meets their expectations.
- Offer to warm-transfer to {producer_first} to answer questions or bind coverage.

ESCALATIONS & TRANSFERS:
- Wants to bind / buy: "Excellent! Let me transfer you directly to {producer_first} right now to bind your policy." (Transfer to {transfer_did}).
- Coverage advice / limits: "As an automated assistant, I can't advise on specific coverage limits, but {producer_first} is licensed and right here to advise you. Let me connect you." (Transfer to {transfer_did}).
- Wants human: "Of course! Connecting you to {producer_first} now."
- Not interested / remove: Apologize politely and end call cleanly.
- Agency Callback: {AGENCY_MAIN_CALLBACK_DISPLAY} (say: "{AGENCY_MAIN_CALLBACK_SPOKEN}").
"""
        briefing = f"Robie quote follow-up: {first_name} regarding {lob} quote with {carrier}. Customer is on the line."
        return VoiceScriptPackage(
            first_sentence=first_sentence,
            task_prompt=task_prompt.strip(),
            voicemail_message=vm,
            transfer_phone=transfer_did,
            transfer_briefing=briefing,
            metadata={
                "applicant_id": lead.applicant_id,
                "opportunity_id": opportunity.opportunity_id,
                "cadence_type": CadenceType.QUOTED_PROSPECT.value,
                "touch_number": touch_number,
                "producer_name": producer_name,
                "lob": lob,
                "carrier": carrier,
                "quoted_premium": premium_str,
            },
        )

    @classmethod
    def _build_xdate_script(
        cls,
        touch_number: int,
        first_name: str,
        producer_name: str,
        producer_first: str,
        lob: str,
        opportunity: Opportunity,
        transfer_did: Optional[str],
        lead: ApplicantLead,
    ) -> VoiceScriptPackage:
        if touch_number == 1:  # T-45 days
            first_sentence = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance calling on behalf of {producer_name}. "
                f"We noticed your {lob} policy comes up for renewal in about six weeks, and we wanted to see if you'd like us "
                f"to run a comparison across our top carriers to see if we can save you money?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance calling on behalf of {producer_name}. "
                f"Your {lob} policy renews in about six weeks. If you'd like a free competitive review, "
                f"please call us back at {AGENCY_MAIN_CALLBACK_DISPLAY}. Have a great day!"
            )
        elif touch_number == 2:  # T-30 days
            first_sentence = (
                f"Hi {first_name}, this is Robie with StreetSmart Insurance following up on your upcoming {lob} renewal. "
                f"We're preparing rate comparisons for you — do you have two minutes to confirm any recent changes with {producer_first}?"
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance regarding your upcoming {lob} renewal. "
                f"Please give our office a quick call back at {AGENCY_MAIN_CALLBACK_DISPLAY} so we can finish reviewing your rates. Thank you!"
            )
        else:  # Touch 3 (T-14 days)
            first_sentence = (
                f"Hi {first_name}, this is Robie with StreetSmart Insurance. With your {lob} renewal about two weeks away, "
                f"I wanted to connect you with {producer_first} to review your rate comparison before your current policy auto-renews."
            )
            vm = (
                f"Hi {first_name}, this is Robie from StreetSmart Insurance regarding your {lob} renewal in two weeks. "
                f"Give {producer_name} a call back at {AGENCY_MAIN_CALLBACK_DISPLAY} to review your savings options. Thanks!"
            )

        task_prompt = f"""
You are Robie from StreetSmart Insurance calling on behalf of {producer_name}.
You are calling {first_name} about their upcoming {lob} policy renewal (X-date).

GOAL:
- Remind them their current policy renews soon.
- Offer a complimentary market comparison across top insurance carriers.
- If interested, warm-transfer to {producer_first} at {transfer_did}.

RULES:
- Do not claim their current policy will cancel or increase unless verified.
- If interested in quoting, transfer to {producer_first}.
- Agency Callback: {AGENCY_MAIN_CALLBACK_DISPLAY} (say: "{AGENCY_MAIN_CALLBACK_SPOKEN}").
- Opt-out: End call politely and immediately.
"""
        briefing = f"Robie X-Date outreach: {first_name} upcoming {lob} renewal. Connected for quote review."
        return VoiceScriptPackage(
            first_sentence=first_sentence,
            task_prompt=task_prompt.strip(),
            voicemail_message=vm,
            transfer_phone=transfer_did,
            transfer_briefing=briefing,
            metadata={
                "applicant_id": lead.applicant_id,
                "opportunity_id": opportunity.opportunity_id,
                "cadence_type": CadenceType.XDATE_OPPORTUNITY.value,
                "touch_number": touch_number,
                "producer_name": producer_name,
                "lob": lob,
            },
        )
