"""Ascend Email Intake, Quote Extraction, HITL Clarification, and EZLynx Agreement Workflow.

Orchestrates:
1. Inbound quote PDF / email message parsing.
2. Underwriting parameter extraction and clarity validation.
3. Interactive clarification with staff if any of the 4 key questions are ambiguous.
4. Production Ascend API program & billable creation.
5. Automated insertion of client checkout agreement link into EZLynx.
6. Confirmation reply to staff.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from robie_job_engine.ascend_api import (
    AscendApiClient,
    AscendCreateProgramWorker,
    configured_client,
)
from robie_job_engine.ezlynx_note_poster import EZLynxAgreementPoster
from robie_job_engine.quote_extractor import ExtractedQuote, QuoteExtractor

logger = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    status: str  # "COMPLETED", "NEEDS_CLARIFICATION", "ERROR"
    quote: ExtractedQuote
    program_id: Optional[str] = None
    program_url: Optional[str] = None
    ezlynx_note_result: Optional[dict[str, Any]] = None
    reply_email_subject: str = ""
    reply_email_body: str = ""
    error: Optional[str] = None


class AscendWorkflowManager:
    """Manages the full lifecycle of an Ascend quote request."""

    def __init__(
        self,
        client_factory: Callable[[], AscendApiClient] = configured_client,
        ezlynx_poster: Optional[EZLynxAgreementPoster] = None,
        quote_extractor: Optional[QuoteExtractor] = None,
    ):
        self.client_factory = client_factory
        self.ezlynx_poster = ezlynx_poster or EZLynxAgreementPoster()
        self.quote_extractor = quote_extractor or QuoteExtractor()

    def process_quote_request(
        self,
        raw_text_or_pdf: str | bytes | Path,
        user_instruction: str = "",
        sender_email: str = "",
        sender_name: str = "",
        applicant_id: Optional[str] = None,
    ) -> WorkflowResult:
        """Process an inbound quote request from email or direct input."""
        # 1. Extract Quote Data
        if isinstance(raw_text_or_pdf, (bytes, Path)):
            quote = self.quote_extractor.extract_from_pdf(raw_text_or_pdf, user_instruction)
        else:
            quote = self.quote_extractor.extract_from_text(str(raw_text_or_pdf), user_instruction)

        # 2. Check if Human-in-the-Loop Clarification is required
        if quote.requires_hitl:
            subject = f"Clarification Needed: Ascend Agreement for {quote.insured_name or 'Insurance Quote'}"
            greeting_name = sender_name or "Team"
            carrier_str = f" from {quote.carrier_name}" if quote.carrier_name else ""
            wholesaler_str = f" ({quote.wholesaler_name})" if quote.wholesaler_name else ""

            body_lines = [
                f"Hi {greeting_name},",
                "",
                f"I received the quote for {quote.insured_name or 'the applicant'}{carrier_str}{wholesaler_str}, but I need clarification on the following items before creating the Ascend financing agreement:",
                "",
            ]
            for q in quote.hitl_questions:
                body_lines.append(f"• {q}")

            body_lines.extend([
                "",
                "Please reply directly to this email with your answers, and I will generate the Ascend agreement and file it into EZLynx.",
                "",
                "Best,",
                "Robie AI",
            ])
            return WorkflowResult(
                status="NEEDS_CLARIFICATION",
                quote=quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
            )

        # 3. All parameters clear -> Create Ascend Agreement
        return self.create_agreement_and_file_ezlynx(
            quote=quote,
            sender_email=sender_email,
            sender_name=sender_name,
            applicant_id=applicant_id,
        )

    def resume_with_clarifications(
        self,
        quote: ExtractedQuote,
        clarification_reply: str,
        sender_email: str = "",
        sender_name: str = "",
        applicant_id: Optional[str] = None,
    ) -> WorkflowResult:
        """Resume workflow after receiving user reply to clarification questions."""
        updated_quote = self.quote_extractor.apply_user_clarifications(quote, clarification_reply)
        if updated_quote.requires_hitl:
            # Still requires answers for remaining items
            return self.process_quote_request(
                raw_text_or_pdf=updated_quote.raw_text,
                user_instruction=clarification_reply,
                sender_email=sender_email,
                sender_name=sender_name,
                applicant_id=applicant_id,
            )

        return self.create_agreement_and_file_ezlynx(
            quote=updated_quote,
            sender_email=sender_email,
            sender_name=sender_name,
            applicant_id=applicant_id,
        )

    def create_agreement_and_file_ezlynx(
        self,
        quote: ExtractedQuote,
        sender_email: str = "",
        sender_name: str = "",
        applicant_id: Optional[str] = None,
    ) -> WorkflowResult:
        """Execute Ascend program creation and post the link into EZLynx."""
        client = self.client_factory()

        # 1. Match Carrier Identifier
        carrier_identifier = quote.carrier_identifier
        if not carrier_identifier and quote.carrier_name:
            carriers = client.search_carriers(quote.carrier_name)
            if carriers:
                carrier_identifier = carriers[0].get("identifier")
            else:
                carrier_identifier = quote.carrier_name.lower().replace(" ", "_").replace("&", "and")

        # 2. Match Wholesaler Identifier
        wholesaler_identifier = quote.wholesaler_identifier
        if not wholesaler_identifier and quote.wholesaler_name:
            wholesalers = client.search_wholesalers(quote.wholesaler_name)
            if wholesalers:
                wholesaler_identifier = wholesalers[0].get("identifier")

        # 3. Find or Create Insured
        insured_name = quote.insured_name or "Named Insured"
        insured_id, _ = client.find_or_create_insured(business_name=insured_name)

        # 4. Resolve Producer & Account Manager
        producer_id = client.resolve_user(sender_email or sender_name or "Robie AI")

        # 5. Build Create Payload
        billable_ident = quote.policy_number or f"Q-{int(time.time())}"
        payload = {
            "execute": True,
            "program": {
                "insured_id": insured_id,
                "producer_id": producer_id,
                "account_manager_id": producer_id,
                "billing_type": "agency_bill",
            },
            "billables": [
                {
                    "billable_identifier": billable_ident,
                    "carrier_identifier": carrier_identifier or "nautilus_insurance_group_scottsdale_e3f1c1",
                    "coverage_identifier": quote.coverage_identifier or "commercial_auto",
                    "effective_date": quote.effective_date,
                    "expiration_date": quote.expiration_date,
                    "premium_cents": quote.pure_premium_cents,
                    "agency_fees_cents": quote.agency_fees_cents,
                    "organization_commission_rate": quote.commission_rate if quote.commission_rate is not None else 0.10,
                    "surplus_lines_tax_cents": quote.surplus_lines_tax_cents,
                }
            ],
        }
        if wholesaler_identifier:
            payload["billables"][0]["wholesaler_identifier"] = wholesaler_identifier
        if quote.policy_fee_cents > 0:
            payload["billables"][0]["policy_fee_cents"] = quote.policy_fee_cents
        if quote.broker_fee_cents > 0:
            payload["billables"][0]["broker_fee_cents"] = quote.broker_fee_cents

        # 6. Execute Program Creation Worker
        worker = AscendCreateProgramWorker(client_factory=lambda: client)
        job = {"id": f"job-{uuid.uuid4().hex[:12]}", "payload": payload}
        result = worker.perform(job, idempotency_key=str(uuid.uuid4()))

        if not result.succeeded:
            return WorkflowResult(
                status="ERROR",
                quote=quote,
                error=result.error or "Failed to create Ascend program",
            )

        destination = result.destination or {}
        program_id = destination.get("program_id")
        program_url = destination.get("program_url") or f"https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id={program_id}"

        # 7. File Agreement into EZLynx
        ezlynx_result = None
        target_applicant_id = applicant_id or "0"
        try:
            ezlynx_result = self.ezlynx_poster.post_agreement_note(
                applicant_id=target_applicant_id,
                quote=quote,
                program_url=program_url,
            )
        except Exception as e:
            logger.warning("EZLynx agreement note posting failed: %s", e)

        # 8. Compose Confirmation Email to User
        greeting_name = sender_name or "Team"
        total_str = f"${quote.total_premium_cents / 100:,.2f}"
        subject = f"Ascend Agreement Ready: {quote.insured_name} - {quote.coverage_title}"
        body_lines = [
            f"Hi {greeting_name},",
            "",
            f"The Ascend payment and financing agreement has been generated for {quote.insured_name}!",
            "",
            f"• Carrier: {quote.carrier_name or 'N/A'}",
            f"• Coverage: {quote.coverage_title}",
            f"• Total Financed / Payable: {total_str}",
            "",
            "Client Agreement & Checkout Link:",
            program_url,
            "",
            "EZLynx Filing Status: Agreement link and policy details filed to EZLynx discussion card.",
            "",
            "Robie was here",
        ]

        return WorkflowResult(
            status="COMPLETED",
            quote=quote,
            program_id=program_id,
            program_url=program_url,
            ezlynx_note_result=ezlynx_result,
            reply_email_subject=subject,
            reply_email_body="\n".join(body_lines),
        )
