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


def _resolve_insured_from_ezlynx(
    applicant_id: Optional[str],
    insured_name: str,
) -> tuple[Optional[dict[str, str]], Optional[dict[str, str]]]:
    """Pull the client's mailing address + primary contact from EZLynx.

    Ascend rejects new-insured creation without a mailing address and an
    insured_contacts entry, and the quote email rarely carries either. The
    client's EZLynx applicant record is the authoritative source.

    Returns (address, contact) dicts, or (None, None) when nothing usable
    was found. Address keys are Ascend's mailing_address_* fields; contact
    keys are first_name/last_name/email/phone.
    """
    try:
        import os
        import sys

        for extra_path in (
            "/opt/renewal-automation-system",
            "/opt/renewal-automation-system/venv/lib/python3.12/site-packages",
            "/opt/renewal-automation-system/venv/lib/python3.11/site-packages",
        ):
            if os.path.exists(extra_path) and extra_path not in sys.path:
                sys.path.append(extra_path)
        from src.ezlynx.api_client import EZLynxApiClient
    except Exception as exc:
        logger.warning("EZLynx applicant lookup unavailable; cannot enrich insured: %s", exc)
        return None, None
    try:
        client = EZLynxApiClient()
        app: Optional[dict[str, Any]] = None
        if applicant_id:
            res = client.get_applicant(str(applicant_id))
            if res.get("status") == "success":
                app = res.get("applicant") or {}
        if not app and insured_name and insured_name != "Named Insured":
            for hit in client.search_applicants(insured_name) or []:
                aid = hit.get("applicant_id") or hit.get("Id")
                if not aid:
                    continue
                res = client.get_applicant(str(aid))
                if res.get("status") == "success":
                    app = res.get("applicant") or {}
                    break
        if not app:
            return None, None
        addr = app.get("CurrentAddress") or {}
        address: Optional[dict[str, str]] = {
            "mailing_address_street_one": addr.get("AddressLine1") or "",
            "mailing_address_city": addr.get("City") or "",
            "mailing_address_state": addr.get("State") or "",
            "mailing_address_zip_code": addr.get("Zip") or "",
        }
        if not all(address.values()):
            address = None
        contact: Optional[dict[str, str]] = {
            "first_name": app.get("FirstName") or "",
            "last_name": app.get("LastName") or "",
            "email": app.get("BusinessEmail") or app.get("Email") or "",
            "phone": app.get("BusinessPhone") or app.get("CellPhone") or "",
        }
        if not (contact["first_name"] or contact["last_name"]):
            contact = None
        return address, contact
    except Exception as exc:
        logger.warning("EZLynx insured enrichment failed: %s", exc)
        return None, None


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

        # 0. Scanned PDF guard: if the quote came from a PDF with no selectable
        # text, say so plainly instead of asking for every field one by one.
        if "[PDF_NO_TEXT_EXTRACTED" in (quote.raw_text or ""):
            subject = f"Couldn't read the attached quote: {quote.insured_name or 'Insurance Quote'}"
            body_lines = [
                f"Hi {sender_name or 'Team'},",
                "",
                "The attached PDF appears to be a scanned image with no selectable text, "
                "so I couldn't read the quote from it. I haven't created the agreement.",
                "",
                "• Could you reply with the quote details as text (carrier, coverage, premium, "
                "agency fee, commission rate, effective date, and producer)?",
                "  (Or attach a text-based PDF and I will read it directly.)",
                "",
                "Best,",
                "Robie AI",
            ]
            return WorkflowResult(
                status="NEEDS_CLARIFICATION",
                quote=quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
            )

        # 1. Match Carrier Identifier. Never invent one: a guessed identifier
        # 422s ("Carrier is invalid") and the failure used to be reported as
        # success. No match -> ask the sender. Multiple matches -> also ask:
        # silently taking the first hit misattributes the agreement.
        carrier_identifier = quote.carrier_identifier
        if not carrier_identifier and quote.carrier_name:
            carriers = client.search_carriers(quote.carrier_name)
            if len(carriers) == 1:
                carrier_identifier = carriers[0].get("identifier")
            elif len(carriers) > 1:
                options = "\n".join(
                    f"  • {c.get('title') or c.get('name') or '(untitled)'}"
                    for c in carriers[:8]
                )
                subject = f"Which carrier for Ascend agreement: {quote.insured_name}"
                body_lines = [
                    f"Hi {sender_name or 'Team'},",
                    "",
                    f"\"{quote.carrier_name}\" matched {len(carriers)} carriers in Ascend, "
                    "so I haven't created the agreement.",
                    "",
                    options,
                    "",
                    "• Which one is correct? (Reply with the exact name or carrier identifier.)",
                    "",
                    "Please reply directly to this email with your answer, and I will generate the Ascend agreement and file it into EZLynx.",
                    "",
                    "Best,",
                    "Robie AI",
                ]
                return WorkflowResult(
                    status="NEEDS_CLARIFICATION",
                    quote=quote,
                    reply_email_subject=subject,
                    reply_email_body="\n".join(body_lines),
                )
        if not carrier_identifier:
            subject = f"Need carrier name for Ascend agreement: {quote.insured_name}"
            body_lines = [
                f"Hi {sender_name or 'Team'},",
                "",
                f"I couldn't match the carrier \"{quote.carrier_name or '(none given)'}\" in Ascend "
                "(search returned no results), so I haven't created the agreement.",
                "",
                "• What is the exact carrier name as it appears in Ascend (or the carrier identifier)?",
                "",
                "Please reply directly to this email with your answer, and I will generate the Ascend agreement and file it into EZLynx.",
                "",
                "Best,",
                "Robie AI",
            ]
            return WorkflowResult(
                status="NEEDS_CLARIFICATION",
                quote=quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
            )

        # 2. Match Wholesaler Identifier. A named wholesaler that doesn't match
        # is asked about, not silently dropped: dropping it could misattribute
        # commission on the billable.
        wholesaler_identifier = quote.wholesaler_identifier
        if not wholesaler_identifier and quote.wholesaler_name:
            clean_wholesaler = quote.wholesaler_name.split("|")[0].strip()
            wholesalers = client.search_wholesalers(clean_wholesaler)
            if len(wholesalers) == 1:
                wholesaler_identifier = wholesalers[0].get("identifier")
            else:
                # No match or ambiguous: ask rather than silently dropping a
                # named wholesaler.
                subject = f"Need wholesaler for Ascend agreement: {quote.insured_name}"
                body_lines = [
                    f"Hi {sender_name or 'Team'},",
                    "",
                    f"I couldn't match the wholesaler \"{quote.wholesaler_name}\" in Ascend, "
                    "so I haven't created the agreement.",
                    "",
                    "• What is the exact wholesaler name as it appears in Ascend (or the wholesaler identifier)?",
                    "  (Or reply \"no wholesaler\" and I will proceed without one.)",
                    "",
                    "Please reply directly to this email with your answer, and I will generate the Ascend agreement and file it into EZLynx.",
                    "",
                    "Best,",
                    "Robie AI",
                ]
                return WorkflowResult(
                    status="NEEDS_CLARIFICATION",
                    quote=quote,
                    reply_email_subject=subject,
                    reply_email_body="\n".join(body_lines),
                )

        # 3. Resolve Producer & Account Manager. Never substitute: an unmatched
        # sender used to silently become Robie AI via a hardcoded user id,
        # misattributing the agreement. The sender is tried first, then a
        # producer name/email from a clarification reply. No match -> ask the
        # sender. This runs before any Ascend write so nothing is created
        # with a guessed producer.
        producer_id: Optional[str] = None
        if sender_email or sender_name:
            producer_id = client.resolve_user(sender_email or sender_name)
        if not producer_id and quote.producer_hint:
            producer_id = client.resolve_user(quote.producer_hint)
        if not producer_id and not (sender_email or sender_name):
            # System-triggered job with no sender: attribute to Robie AI itself.
            producer_id = client.resolve_user("Robie AI")
        if not producer_id:
            unmatched = quote.producer_hint or sender_email or sender_name or "(unknown sender)"
            subject = f"Need producer for Ascend agreement: {quote.insured_name or 'Insurance Quote'}"
            body_lines = [
                f"Hi {sender_name or 'Team'},",
                "",
                f"I couldn't match \"{unmatched}\" to an Ascend user, so I haven't created the agreement.",
                "",
                "• Who should be listed as the producer and account manager on this Ascend agreement? (Reply with the name or Ascend user email.)",
                "",
                "Please reply directly to this email with your answer, and I will generate the Ascend agreement and file it into EZLynx.",
                "",
                "Best,",
                "Robie AI",
            ]
            return WorkflowResult(
                status="NEEDS_CLARIFICATION",
                quote=quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
            )

        # 4. Find or Create Insured. Ascend requires a mailing address and a
        # primary contact for new insureds; the quote email rarely carries
        # either. Prefer values the sender supplied in a clarification reply;
        # otherwise pull both from the client's EZLynx applicant record (a
        # sender-supplied applicant id in the reply retries the lookup with
        # that id). If neither source yields them, ask the sender.
        insured_name = quote.insured_name or "Named Insured"
        effective_applicant_id = quote.applicant_id_hint or applicant_id
        ezlynx_address, ezlynx_contact = _resolve_insured_from_ezlynx(
            effective_applicant_id, insured_name
        )
        address = quote.mailing_address or ezlynx_address
        contact = quote.primary_contact or ezlynx_contact
        if not address or not contact:
            subject = f"Need client address for Ascend agreement: {insured_name}"
            body_lines = [
                f"Hi {sender_name or 'Team'},",
                "",
                f"I couldn't find a mailing address and primary contact for \"{insured_name}\" "
                "in EZLynx, and Ascend requires both to create the insured. I haven't created the agreement.",
                "",
                "• What is the client's mailing address (street, city, state, zip)?",
                "• Who is the primary contact (name, email, phone)?",
                "  (Or include the client's EZLynx applicant link in your reply and I'll pull it from there.)",
                "",
                "Please reply directly to this email with your answers, and I will generate the Ascend agreement and file it into EZLynx.",
                "",
                "Best,",
                "Robie AI",
            ]
            return WorkflowResult(
                status="NEEDS_CLARIFICATION",
                quote=quote,
                reply_email_subject=subject,
                reply_email_body="\n".join(body_lines),
            )
        insured_id, _ = client.find_or_create_insured(
            business_name=insured_name,
            address=address,
            contact=contact,
        )

        # 5. Build Create Payload
        # Duplicate guard: if a program already exists for this policy number,
        # ask before creating another. Resubmits happen (forwarded twice,
        # retried after a timeout); a silent second program double-bills.
        # A sender-confirmed duplicate ("confirm duplicate" in a reply) bypasses.
        # For multi-LOB quotes, check EVERY sub-policy's policy number.
        policy_numbers_to_check: list[str] = []
        if quote.sub_policies:
            for sp in quote.sub_policies:
                pn = sp.get("policy_number") or quote.policy_number
                if pn and pn not in policy_numbers_to_check:
                    policy_numbers_to_check.append(pn)
        elif quote.policy_number:
            policy_numbers_to_check.append(quote.policy_number)
        if policy_numbers_to_check and not quote.duplicate_confirmed:
            for pn in policy_numbers_to_check:
                existing = client.find_program_by_policy(pn)
                if existing:
                    # Verify the insured name matches before treating as duplicate.
                    # A policy number collision (quote ref vs actual policy) with a
                    # different insured is NOT a duplicate.
                    existing_program = existing.get("program", {})
                    existing_insured = existing_program.get("insured", {})
                    existing_name = (
                        existing_insured.get("legal_name")
                        or existing_insured.get("business_name")
                        or existing_insured.get("name")
                        or ""
                    ).strip().lower()
                    quote_name = (quote.insured_name or "").strip().lower()
                    if existing_name and quote_name and existing_name != quote_name:
                        # Different insured — not a true duplicate, continue checking
                        continue
                    existing_id = existing.get("id") or existing.get("program_id")
                    subject = f"Agreement already exists for policy {pn}"
                    body_lines = [
                        f"Hi {sender_name or 'Team'},",
                        "",
                        f"There is already an Ascend agreement for policy \"{pn}\" "
                        f"(program {existing_id}), so I haven't created another one.",
                        "",
                        "• If you meant to create a second agreement for this policy, reply \"confirm duplicate\" "
                        "and I will proceed.",
                        "",
                        "Best,",
                        "Robie AI",
                    ]
                    return WorkflowResult(
                        status="NEEDS_CLARIFICATION",
                        quote=quote,
                        program_id=str(existing_id) if existing_id else None,
                        reply_email_subject=subject,
                        reply_email_body="\n".join(body_lines),
                    )
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
                # Validate: each sub-policy must have a positive premium.
                # A zero/missing premium means extraction failed for this LOB;
                # fail closed rather than creating a $0 billable.
                b_prem = sp.get("pure_premium_cents", 0)
                if b_prem <= 0:
                    return WorkflowResult(
                        status="ERROR",
                        quote=quote,
                        reply_email_subject=f"Missing premium for {sp.get('title') or sp.get('coverage_identifier')}: {quote.insured_name}",
                        reply_email_body=(
                            f"Hi {sender_name or 'Team'},\n\n"
                            f"I found multiple lines of business in the quote for {quote.insured_name}, "
                            f"but I couldn't determine the premium for "
                            f"\"{sp.get('title') or sp.get('coverage_identifier')}\". "
                            f"I haven't created the agreement.\n\n"
                            f"• What is the premium for this line of business?\n\n"
                            "Best,\nRobie AI"
                        ),
                    )
                # Per-sub-policy carrier: writing carrier first, then carrier
                # name, then fall back to the parent quote's resolved carrier.
                # Different LOBs can be written by different carriers.
                sp_carrier_identifier = sp.get("carrier_identifier")
                if not sp_carrier_identifier:
                    # Per-sub-policy carrier: its own writing carrier or carrier
                    # name. Do NOT fall back to quote.writing_carrier_name —
                    # that field is document-global and would bleed one LOB's
                    # writing carrier into every other LOB.
                    sp_carrier_name = (
                        sp.get("writing_carrier_name")
                        or sp.get("carrier_name")
                    )
                    if sp_carrier_name and sp_carrier_name != quote.carrier_name:
                        sp_carriers = client.search_carriers(sp_carrier_name)
                        if len(sp_carriers) == 1:
                            sp_carrier_identifier = sp_carriers[0].get("identifier")
                        # 0 or 2+ matches: fall through to parent carrier;
                        # the parent's fail-closed logic already handled the
                        # ambiguous case.
                if not sp_carrier_identifier:
                    sp_carrier_identifier = carrier_identifier  # parent resolved
                # Per-sub-policy policy number, dates; fall back to parent.
                sp_policy_number = sp.get("policy_number") or quote.policy_number
                suffix = sp.get("billable_suffix") or str(i + 1)
                b_ident = f"{sp_policy_number}-{suffix}" if sp_policy_number else f"{billable_ident}-{suffix}"
                b_cov = sp.get("coverage_identifier") or quote.coverage_identifier or "commercial_auto"
                b_pol_fee = sp.get("policy_fee_cents", 0)
                b_tax = sp.get("taxes_and_fees_cents") or sp.get("surplus_lines_tax_cents", 0)
                b_eff = sp.get("effective_date") or quote.effective_date
                b_exp = sp.get("expiration_date") or quote.expiration_date
                b_comm = sp.get("commission_rate")
                if b_comm is None:
                    b_comm = quote.commission_rate if quote.commission_rate is not None else 0.10
                # Apply agency fee to primary (first) policy so it is charged once on the agreement
                b_agency_fee = quote.agency_fees_cents if i == 0 else 0

                billable = {
                    "billable_identifier": b_ident,
                    "carrier_identifier": sp_carrier_identifier,  # never guessed; resolved above
                    "coverage_identifier": b_cov,
                    "effective_date": b_eff,
                    "expiration_date": b_exp,
                    "premium_cents": b_prem,
                    "agency_fees_cents": b_agency_fee,
                    "organization_commission_rate": b_comm,
                    "taxes_and_fees_cents": b_tax,
                }
                if wholesaler_identifier:
                    billable["wholesaler_identifier"] = wholesaler_identifier
                if b_pol_fee > 0:
                    billable["policy_fee_cents"] = b_pol_fee
                payload["billables"].append(billable)
        else:
            # Financed premium: subtract any down payment already paid to the
            # carrier so the client is never double-charged.
            financed_premium = max(0, quote.pure_premium_cents - quote.down_payment_cents)
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
                "carrier_identifier": carrier_identifier,  # guaranteed non-empty above; never guess
                "coverage_identifier": quote.coverage_identifier or "commercial_auto",
                "effective_date": quote.effective_date,
                "expiration_date": exp_date or quote.expiration_date,
                "premium_cents": financed_premium,
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

        # Defense in depth: even a "successful" worker result must carry every
        # expected billable before we claim the agreement is ready. A program
        # with missing billables is not a completed agreement.
        detail = result.detail or {}
        billable_ids = detail.get("billable_ids") or []
        expected_billables = detail.get("expected_billable_count")
        destination = result.destination or {}
        if expected_billables is not None and len(billable_ids) != expected_billables:
            return WorkflowResult(
                status="ERROR",
                quote=quote,
                error=(
                    f"Ascend created program {destination.get('program_id')} with "
                    f"{len(billable_ids)} of {expected_billables} billables. "
                    "The agreement is NOT ready; do not send a checkout link."
                ),
            )

        destination = result.destination or {}
        program_id = destination.get("program_id")
        program_url = destination.get("program_url") or f"https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id={program_id}"

        # 7. File Agreement into EZLynx
        ezlynx_result = None
        # Only attempt EZLynx filing with a valid applicant ID.
        # "0" is not valid — skip filing and report NOT filed instead of failing silently.
        target_applicant_id = applicant_id
        if target_applicant_id and target_applicant_id != "0":
            try:
                ezlynx_result = self.ezlynx_poster.post_agreement_note(
                    applicant_id=target_applicant_id,
                    quote=quote,
                    program_url=program_url,
                )
            except Exception as e:
                logger.warning("EZLynx agreement note posting failed: %s", e)
        else:
            logger.warning(
                "Skipping EZLynx filing: no valid applicant_id provided for %s",
                quote.insured_name,
            )

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
            (
                "EZLynx Filing Status: Agreement link and policy details filed to EZLynx discussion card."
                if ezlynx_result
                else "EZLynx Filing Status: NOT filed — the EZLynx note failed to post. "
                     "The agreement is ready but needs manual filing to EZLynx."
            ),
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

