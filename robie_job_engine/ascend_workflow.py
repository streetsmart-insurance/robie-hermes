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
        clarification_attempts: int = 1,
    ) -> WorkflowResult:
        """Resume workflow after receiving user reply to clarification questions."""
        updated_quote = self.quote_extractor.apply_user_clarifications(quote, clarification_reply)
        if updated_quote.requires_hitl:
            greeting_name = sender_name or (sender_email.split("@")[0].title() if sender_email else "Team")

            # Hard Circuit Breaker: If clarification attempts >= 2, escalate to EZLynx CSR task
            if clarification_attempts >= 2:
                logger.warning(
                    "Ascend clarification circuit breaker tripped (attempts=%d) for %s. Escalating to EZLynx task.",
                    clarification_attempts,
                    updated_quote.insured_name,
                )
                escalation_subject = f"Escalated to CSR: Ascend Agreement for {updated_quote.insured_name or 'Insurance Quote'}"
                unresolved_items = [q.split(":")[0].strip() for q in updated_quote.hitl_questions] or updated_quote.hitl_reasons

                ezlynx_task_res = None
                try:
                    if self.ezlynx_poster:
                        task_desc = (
                            f"Manual Ascend Agreement Review Required for {updated_quote.insured_name or 'Applicant'}.\n\n"
                            f"Carrier: {updated_quote.carrier_name or 'Unspecified'}\n"
                            f"Pure Premium: ${updated_quote.pure_premium_cents / 100:,.2f}\n"
                            f"Pending Clarifications:\n" + "\n".join(f"- {q}" for q in updated_quote.hitl_questions) + "\n\n"
                            f"Robie was here"
                        )
                        target_applicant = applicant_id
                        if not target_applicant and updated_quote.insured_name:
                            target_applicant = self.ezlynx_poster.find_applicant_by_name(updated_quote.insured_name)

                        if target_applicant:
                            ezlynx_task_res = self.ezlynx_poster.create_cancellation_task(
                                applicant_id=str(target_applicant),
                                title=f"Ascend Financing Agreement Review - {updated_quote.insured_name or 'Quote'}",
                                description=task_desc,
                            )
                except Exception as exc:
                    logger.warning("Could not auto-create EZLynx escalation task: %s", exc)

                escalation_body = (
                    f"Hi {greeting_name},\n\n"
                    f"I received your response regarding {updated_quote.insured_name or 'the quote'}, but I was unable to fully confirm all remaining items ({', '.join(unresolved_items)}) after {clarification_attempts} clarification attempts.\n\n"
                    f"To prevent any delays, I have stopped automated clarification and escalated this to our CSR team in EZLynx for manual completion.\n\n"
                    f"Best,\n"
                    f"Robie AI"
                )
                return WorkflowResult(
                    status="ESCALATED",
                    quote=updated_quote,
                    reply_email_subject=escalation_subject,
                    reply_email_body=escalation_body,
                    ezlynx_note_result=ezlynx_task_res,
                )

            # Still within attempt limit (< 2): Send concise prompt with ONLY remaining questions
            subject = f"Clarification Needed: Ascend Agreement for {updated_quote.insured_name or 'Insurance Quote'}"
            body_lines = [
                f"Hi {greeting_name},",
                "",
                f"Thank you. I still need clarification on the following remaining item(s) before creating the Ascend financing agreement:",
                "",
            ]
            for q in updated_quote.hitl_questions:
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
                quote=updated_quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
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
            clean_wholesaler = quote.wholesaler_name.split("|")[0].strip()
            wholesalers = client.search_wholesalers(clean_wholesaler)
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
            "billables": [],
        }

        if quote.sub_policies:
            for i, sp in enumerate(quote.sub_policies):
                suffix = sp.get("billable_suffix") or str(i + 1)
                b_ident = f"{billable_ident}-{suffix}"
                b_cov = sp.get("coverage_identifier") or quote.coverage_identifier or "commercial_auto"
                b_prem = sp.get("pure_premium_cents", 0)
                b_pol_fee = sp.get("policy_fee_cents", 0)
                b_tax = sp.get("taxes_and_fees_cents") or sp.get("surplus_lines_tax_cents", 0)
                # Apply agency fee to primary (first) policy so it is charged once on the agreement
                b_agency_fee = quote.agency_fees_cents if i == 0 else 0

                billable = {
                    "billable_identifier": b_ident,
                    "carrier_identifier": carrier_identifier or "nautilus_insurance_group_scottsdale_e3f1c1",
                    "coverage_identifier": b_cov,
                    "effective_date": quote.effective_date,
                    "expiration_date": quote.expiration_date,
                    "premium_cents": b_prem,
                    "agency_fees_cents": b_agency_fee,
                    "organization_commission_rate": quote.commission_rate if quote.commission_rate is not None else 0.10,
                    "taxes_and_fees_cents": b_tax,
                }
                if wholesaler_identifier:
                    billable["wholesaler_identifier"] = wholesaler_identifier
                if b_pol_fee > 0:
                    billable["policy_fee_cents"] = b_pol_fee
                payload["billables"].append(billable)
        else:
            # 2026-10-02: Default expiration to 12 months from effective if not provided.
            # Most commercial policies are 12-month terms. This avoids API failures
            # when the sender doesn't specify expiration.
            exp_date = quote.expiration_date
            if not exp_date and quote.effective_date:
                try:
                    from datetime import datetime
                    eff = datetime.strptime(quote.effective_date, "%Y-%m-%d")
                    # Add 12 months (handle year rollover)
                    exp_year = eff.year + 1
                    exp_date = f"{exp_year}-{eff.month:02d}-{eff.day:02d}"
                except:
                    pass
            billable = {
                "billable_identifier": billable_ident,
                "carrier_identifier": carrier_identifier or "nautilus_insurance_group_scottsdale_e3f1c1",
                "coverage_identifier": quote.coverage_identifier or "commercial_auto",
                "effective_date": quote.effective_date,
                "expiration_date": exp_date or quote.expiration_date,
                "premium_cents": quote.pure_premium_cents,
                "agency_fees_cents": quote.agency_fees_cents,
                "organization_commission_rate": quote.commission_rate if quote.commission_rate is not None else 0.10,
                "taxes_and_fees_cents": quote.surplus_lines_tax_cents,
            }
            if wholesaler_identifier:
                billable["wholesaler_identifier"] = wholesaler_identifier
            if quote.policy_fee_cents > 0:
                billable["policy_fee_cents"] = quote.policy_fee_cents
            if quote.broker_fee_cents > 0:
                billable["broker_fee_cents"] = quote.broker_fee_cents
            payload["billables"].append(billable)

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
        ]
        if quote.sub_policies:
            body_lines.append("• Separate Itemized Policies on Agreement:")
            for sp in quote.sub_policies:
                title = sp.get("title") or sp.get("coverage_identifier")
                p_cents = sp.get("pure_premium_cents", 0)
                f_cents = sp.get("policy_fee_cents", 0)
                t_cents = sp.get("taxes_and_fees_cents") or sp.get("surplus_lines_tax_cents", 0)
                pol_tot = p_cents + f_cents + t_cents
                body_lines.append(f"  - {title}: ${pol_tot / 100:,.2f} (Base: ${p_cents / 100:,.2f}, MGA Fee: ${f_cents / 100:,.2f}, Taxes: ${t_cents / 100:,.2f})")
            if quote.agency_fees_cents > 0:
                body_lines.append(f"• Broker / Agency Fee: ${quote.agency_fees_cents / 100:,.2f}")
        else:
            body_lines.append(f"• Coverage: {quote.coverage_title}")

        body_lines.extend([
            f"• Total Financed / Payable: {total_str}",
            "",
            "Client Agreement & Checkout Link:",
            program_url,
            "",
            "EZLynx Filing Status: Agreement link and policy details filed to EZLynx discussion card.",
            "",
            "Robie was here",
        ])

        return WorkflowResult(
            status="COMPLETED",
            quote=quote,
            program_id=program_id,
            program_url=program_url,
            ezlynx_note_result=ezlynx_result,
            reply_email_subject=subject,
            reply_email_body="\n".join(body_lines),
        )

    def process_endorsement_request(
        self,
        raw_text_or_pdf: str | bytes | Path,
        applicant_id: Optional[str] = None,
        sender_name: str = "",
    ) -> dict[str, Any]:
        """Process an endorsement or additional premium request."""
        from robie_job_engine.quote_extractor import EndorsementExtractor
        from robie_job_engine.ezlynx_note_poster import format_endorsement_note

        extractor = EndorsementExtractor()
        if isinstance(raw_text_or_pdf, (bytes, Path)):
            endorsement = extractor.extract_from_pdf(raw_text_or_pdf)
        else:
            endorsement = extractor.extract_from_text(str(raw_text_or_pdf))

        if not endorsement.policy_number:
            return {
                "status": "ERROR",
                "error": "Could not identify policy number in endorsement document",
                "endorsement": endorsement,
            }

        client = self.client_factory()
        found = client.find_program_by_policy(endorsement.policy_number)
        if not found:
            return {
                "status": "ERROR",
                "error": f"No active Ascend program found for policy {endorsement.policy_number}",
                "endorsement": endorsement,
            }

        program_id = found["program_id"]
        parent_billable_id = found["parent_billable_id"]
        program_obj = found.get("program") or {}
        program_url = program_obj.get("program_url") or f"https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id={program_id}"

        # Create endorsement billable
        try:
            billable_id, billable_rec = client.create_endorsement_billable(
                program_id=program_id,
                parent_billable_id=parent_billable_id,
                description=endorsement.description,
                premium_cents=endorsement.additional_premium_cents,
                effective_date=endorsement.effective_date,
                taxes_and_fees_cents=endorsement.taxes_and_fees_cents,
                seller_commission_rate=endorsement.seller_commission_rate,
            )
        except Exception as e:
            return {
                "status": "ERROR",
                "error": f"Ascend API error creating endorsement: {e}",
                "endorsement": endorsement,
            }

        # Post note to EZLynx
        prem_str = f"${endorsement.additional_premium_cents / 100:,.2f}"
        tax_str = f"${endorsement.taxes_and_fees_cents / 100:,.2f}" if endorsement.taxes_and_fees_cents else None
        tot_str = f"${endorsement.total_cents / 100:,.2f}"

        note_text = format_endorsement_note(
            insured_name=endorsement.insured_name or program_obj.get("insured", {}).get("business_name", "Insured"),
            policy_number=endorsement.policy_number,
            carrier_name=endorsement.carrier_name or found.get("billable", {}).get("carrier", {}).get("title"),
            wholesaler_name=endorsement.wholesaler_name or found.get("billable", {}).get("wholesaler", {}).get("title"),
            coverage_title=endorsement.coverage_title,
            endorsement_description=endorsement.description,
            effective_date=endorsement.effective_date,
            additional_premium_text=prem_str,
            taxes_and_fees_text=tax_str,
            total_endorsement_text=tot_str,
            endorsement_checkout_url=program_url,
        )

        ezlynx_res = None
        target_applicant_id = applicant_id or "0"
        try:
            ezlynx_res = self.ezlynx_poster.post_custom_note(
                applicant_id=target_applicant_id,
                title=f"Endorsement: {endorsement.description} - Policy #{endorsement.policy_number}",
                note_text=note_text,
                policy_number=endorsement.policy_number,
            )
        except Exception as exc:
            logger.warning("Failed to post endorsement note to EZLynx: %s", exc)

        return {
            "status": "COMPLETED",
            "program_id": program_id,
            "parent_billable_id": parent_billable_id,
            "billable_id": billable_id,
            "program_url": program_url,
            "endorsement": endorsement,
            "ezlynx_result": ezlynx_res,
        }

