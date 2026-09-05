"""
Email Command Dispatcher for Autonomous Voice Calling.
Listens to robie@streetsmart.insurance for call requests from CSRs/Account Managers,
hydrates context from EZLynx and database, acknowledges receipt, and triggers Voice AI.
"""

import logging
import re
from typing import Optional, Dict, Any, List

from src.email_outreach.gmail_client import GmailRenewalClient
from src.voice.context_hydrator import ContextHydrator, CallingDossier
from src.voice.voice_client import CarrierVoiceClient

logger = logging.getLogger("voice_email_dispatcher")

# Authorized agency domains and key accounts
AUTHORIZED_DOMAINS = ["@streetsmart.insurance"]
CALL_KEYWORDS = ["call", "dial", "phone", "ring", "speak with", "contact by phone"]


class EmailCallDispatcher:
    def __init__(
        self,
        gmail_client: Optional[GmailRenewalClient] = None,
        hydrator: Optional[ContextHydrator] = None,
        voice_client: Optional[CarrierVoiceClient] = None,
    ):
        self.gmail = gmail_client or GmailRenewalClient()
        self.hydrator = hydrator or ContextHydrator()
        self.voice = voice_client or CarrierVoiceClient()

    def is_authorized_sender(self, sender_email: str) -> bool:
        """Verifies that the sender is an authorized team member."""
        sender_clean = sender_email.lower().strip()
        return any(domain in sender_clean for domain in AUTHORIZED_DOMAINS)

    def parse_call_command(self, sender: str, subject: str, body: str) -> Optional[Dict[str, Any]]:
        """
        Detects if an incoming email is instructing Robie to call a carrier,
        extracting policy number, phone override, and specific instructions.
        """
        if not self.is_authorized_sender(sender):
            logger.debug(f"Ignoring message from non-agency sender: {sender}")
            return None

        full_text = f"{subject} {body}".strip()
        full_text_lower = full_text.lower()

        # Check for call command intent
        has_call_intent = any(re.search(rf"\b{kw}\b", full_text_lower) for kw in CALL_KEYWORDS)
        if not has_call_intent:
            return None

        # 1. Extract explicit policy number
        policy_number = None
        # Pattern 1: Explicit label "policy #12345", "pol: 12345", "carrier: PWC12345"
        pol_match = re.search(r"(?:policy|pol|policy number|pol#|policy#|carrier)[:\s#]+([A-Z0-9\-]{5,})", full_text, re.IGNORECASE)
        if pol_match:
            candidate = pol_match.group(1).strip()
            if candidate.lower() not in ["underwriting", "renewal", "quote", "terms", "specialty"]:
                policy_number = candidate
        
        if not policy_number:
            # Pattern 2: Hash tag like "#PWC1239278" or "#UB-6N448514"
            hash_match = re.search(r"#([A-Z0-9\-]{5,})", full_text)
            if hash_match:
                policy_number = hash_match.group(1).strip()

        if not policy_number:
            # Pattern 3: Alphanumeric policy pattern containing at least one digit and letter
            tokens = re.findall(r"\b([A-Z0-9]{2,6}[- ]?[A-Z0-9]{4,14}(?:-[0-9]{1,4})?)\b", full_text)
            for tm in tokens:
                if any(c.isdigit() for c in tm) and any(c.isalpha() for c in tm) and len(tm) >= 6:
                    policy_number = tm
                    break

        # 2. Extract phone number override if explicitly provided
        phone_match = re.search(
            r"(?:at|phone|dial|number)[:\s]*([2-9]\d{2}[-.\s]?\d{3}[-.\s]?\d{4}|\([2-9]\d{2}\)\s*\d{3}[-.\s]?\d{4})",
            full_text,
            re.IGNORECASE,
        )
        phone_override = phone_match.group(1).strip() if phone_match else None

        # 3. Extract target applicant or carrier name from context
        applicant_name = None
        app_match = re.search(r"(?:for|regarding|account|client)[:\s]+([A-Za-z0-9\s,&]{3,30}?)(?:\.|\n|,|policy|pol|$)", full_text, re.IGNORECASE)
        if app_match:
            candidate = app_match.group(1).strip()
            if candidate.lower() not in ["the renewal", "renewal", "terms", "the quote", "commercial"]:
                applicant_name = candidate

        # 4. Extract specific instructions (e.g. "ask if...", "check whether...")
        instructions = None
        instr_match = re.search(r"(?:please\s+|ask\s+|check\s+|inquire\s+|verify\s+)(.+?)(?:\.|\n|$)", body, re.IGNORECASE)
        if instr_match:
            instructions = instr_match.group(0).strip()

        call_type = None
        type_match = re.search(
            r"call\s*type\s*:\s*(client(?:[\s_-]*follow[\s_-]*up)?|carrier|existing)\b",
            full_text,
            re.IGNORECASE,
        )
        if type_match:
            from src.voice.context_hydrator import normalize_call_type
            call_type = normalize_call_type(type_match.group(1))

        return {
            "policy_number": policy_number,
            "applicant_name": applicant_name,
            "phone_override": phone_override,
            "instructions": instructions,
            "sender": sender,
            "subject": subject,
            "body": body,
            "call_type": call_type,
        }

    def process_inbound_call_requests(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        """
        Polls unread emails in robie@streetsmart.insurance, detects call commands from CSRs,
        hydrates policy context, replies with acknowledgment, and dispatches the Voice AI.
        """
        if not self.gmail.is_authenticated():
            logger.warning("Gmail client not authenticated; skipping email call dispatch.")
            return []

        # Query unread messages received by Robie
        try:
            res = (
                self.gmail.service.users()
                .messages()
                .list(userId="me", q="is:unread to:robie@streetsmart.insurance", maxResults=20)
                .execute()
            )
            messages = res.get("messages", [])
        except Exception as e:
            logger.error(f"Failed to fetch unread messages for call dispatcher: {e}")
            return []

        dispatched = []

        for m in messages:
            msg_id = m.get("id")
            thread_id = m.get("threadId")
            parsed = self.gmail._fetch_and_parse_msg(self.gmail.service, msg_id, "robie")
            if not parsed:
                continue

            sender = parsed.get("sender", "")
            subject = parsed.get("subject", "")
            body = parsed.get("body", "")

            cmd = self.parse_call_command(sender=sender, subject=subject, body=body)
            if not cmd:
                continue

            logger.info(f"Detected incoming call instruction from {sender}: {subject}")

            # Hydrate complete CallingDossier. Email sender is the requestor
            # (warm-transfer target), not the EZLynx Producer field.
            sender_email = self._extract_clean_email(sender)
            dossier: Optional[CallingDossier] = self.hydrator.hydrate(
                policy_number=cmd.get("policy_number"),
                applicant_name=cmd.get("applicant_name"),
                phone_override=cmd.get("phone_override"),
                instructions=cmd.get("instructions"),
                requester_email=sender_email,
                requestor_name=self._extract_sender_name(sender),
                requestor_email=sender_email,
                call_type=cmd.get("call_type"),
            )
            if dossier:
                self.hydrator.enrich_identity_from_ezlynx(dossier)
                self.hydrator.enrich_identity(
                    dossier,
                    requestor_name=dossier.requestor_name,
                    requestor_email=dossier.requestor_email,
                )

            if not dossier:
                logger.warning(f"Could not hydrate policy context for request: {cmd}")
                self._send_clarification_email(sender=sender, subject=subject, msg_id=msg_id, thread_id=thread_id)
                self.gmail.mark_as_read(msg_id)
                continue

            # 1. Send immediate acknowledgment email to the CSR
            self._send_dispatch_ack(
                dossier=dossier,
                sender=sender,
                subject=subject,
                msg_id=msg_id,
                thread_id=thread_id,
            )

            # 2. Dispatch outbound Voice AI call
            result = self.voice.dispatch_call(dossier=dossier, dry_run=dry_run)

            # 3. Mark request email as read
            self.gmail.mark_as_read(msg_id)

            dispatched.append({
                "message_id": msg_id,
                "sender": sender,
                "policy_number": dossier.policy_number,
                "carrier": dossier.carrier_name,
                "phone": dossier.carrier_phone,
                "dispatch_result": result,
            })

        return dispatched

    def _send_dispatch_ack(
        self,
        dossier: CallingDossier,
        sender: str,
        subject: str,
        msg_id: str,
        thread_id: str,
    ):
        """Sends immediate confirmation to the CSR that Robie is initiating the call."""
        clean_to = self._extract_clean_email(sender)
        ack_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject

        ext_clause = f" (Ext {dossier.carrier_extension})" if dossier.carrier_extension else ""
        instr_clause = (
            f"\n- **Special Instructions:** {dossier.custom_instructions}"
            if dossier.custom_instructions
            else ""
        )

        ack_body = f"""Hi there,

I received your request to call carrier underwriting. Here is the dossier I've assembled:

- **Insured Account:** {dossier.insured_name}
- **Policy Number:** #{dossier.policy_number} ({dossier.line_of_business})
- **Carrier:** {dossier.carrier_name}
- **Dialing Phone:** {dossier.carrier_phone or 'Resolving...'}{ext_clause}
- **Agency Producer Code:** {dossier.agency_code or 'StreetSmart Insurance'}{instr_clause}

I am initiating the outbound call now. Once the call concludes, I will automatically:
1. Thread the full call summary, audio recording, and terms into the EZLynx Discussion Card.
2. Reply directly to this email with the outcome and audio link.

Best regards,
Robie
StreetSmart Insurance Operations Engine
"""
        self.gmail.send_email(
            to_email=clean_to,
            subject=ack_subject,
            body_text=ack_body,
            in_reply_to=msg_id,
            thread_id=thread_id,
        )

    def _send_clarification_email(self, sender: str, subject: str, msg_id: str, thread_id: str):
        """Asks the sender to specify a policy number or insured name if none was found."""
        clean_to = self._extract_clean_email(sender)
        body = """Hi there,

I received your request to make a phone call, but I couldn't identify the policy number or client name in your message.

Please reply with the Policy Number (e.g. 'PWC1239278') or Insured Name, and I'll look up the carrier details, producer code, and place the call right away!

Best regards,
Robie
StreetSmart Insurance Operations Engine
"""
        self.gmail.send_email(
            to_email=clean_to,
            subject=f"Re: {subject}",
            body_text=body,
            in_reply_to=msg_id,
            thread_id=thread_id,
        )

    @staticmethod
    def _extract_clean_email(raw_sender: str) -> str:
        match = re.search(r"<([^>]+)>", raw_sender)
        return match.group(1).strip() if match else raw_sender.strip()

    @staticmethod
    def _extract_sender_name(raw_sender: str) -> Optional[str]:
        """Parse 'Mike Sosa <mike@streetsmart.insurance>' display name."""
        match = re.match(r"^\s*\"?([^\"<]+?)\"?\s*<", raw_sender or "")
        if not match:
            return None
        name = match.group(1).strip()
        if name and "@" not in name:
            return name
        return None
