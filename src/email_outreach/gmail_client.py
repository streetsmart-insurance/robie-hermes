"""Gmail API Client for Robie outreach and dual-inbox polling (Robie + Hello)."""

import os
import base64
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from googleapiclient.discovery import Resource

from src.config import settings
from src.email_outreach.auth_setup import (
    get_robie_gmail_service,
    get_hello_gmail_service,
    get_all_active_inbox_services
)

logger = logging.getLogger("gmail_client")

DEFAULT_RENEWAL_POLL_INBOXES = (
    "robie@streetsmart.insurance",
    "hello@streetsmart.insurance",
)


def filter_renewal_poll_inboxes(
    inbox_services: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Restrict Gmail services to robie@ and hello@ only.

    OTP / 2FA may still use Carlo's mailbox via ``otp_interceptor``. Renewal
    reply polling must never scan CSR or org-wide inboxes.
    """
    allowed = {
        str(e).strip().lower()
        for e in (getattr(settings, "gmail_poll_inboxes", None) or DEFAULT_RENEWAL_POLL_INBOXES)
        if e
    }
    if not allowed:
        allowed = set(DEFAULT_RENEWAL_POLL_INBOXES)
    filtered: Dict[str, Any] = {}
    for name, svc in (inbox_services or {}).items():
        if name and str(name).strip().lower() in allowed:
            filtered[name] = svc
    return filtered

def extract_clean_reply_text(raw_text: str) -> str:
    """Extracts the genuine reply message from an underwriter, stripping out
    headers, signature blocks, disclaimers, and quoted chain text.
    """
    if not raw_text:
        return ""
    import re
    # Normalize line breaks
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Cut off standard forward/reply chain markers
    markers = [
        r"-----Original Message-----",
        r"----- Forwarded Message -----",
        r"From:.*Sent:.*To:",
        r"On\s+.*wrote:\s*$",
        r"________________________________",
    ]
    for m in markers:
        parts = re.split(m, text, flags=re.IGNORECASE | re.MULTILINE)
        if len(parts) > 1:
            text = parts[0]

    # 2. Cut off standard signature blocks, footers, and disclaimers
    sig_patterns = [
        r"\n\s*--\s*\n.*",
        r"\n\s*(?:Best regards|Regards|Sincerely|Thank you|Thanks|Warm regards|Cheers),?\s*\n.*",
        r"\n\s*[A-Z][a-z]+ [A-Z][a-z]+\s*\n\s*(?:Director|President|VP|Underwriting|Strategic|Commercial|Account|Manager|Customer).*?",
        r"\n\s*(?:PHONE:|Direct:|Fax:|Email:|Cell:|Tel:).*?",
        r"\n\s*CONFIDENTIALITY NOTICE:?.*",
        r"\n\s*This (?:email|message|communication) (?:and any files|is intended).*",
        r"\n\s*Notice: This communication.*",
        r"\n\s*Markel – Loss Run Reports.*",
    ]
    for sp in sig_patterns:
        text = re.split(sp, text, flags=re.IGNORECASE | re.DOTALL)[0]

    # 3. Clean lines, remove quoted lines starting with '>'
    lines = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith(">")]
    return " ".join(lines)

class GmailRenewalClient:
    """Manages email sending from Robie, and reply polling across Robie and Hello inboxes."""

    def __init__(
        self,
        service: Optional[Resource] = None,
        inbox_services: Optional[Dict[str, Resource]] = None
    ):
        # Hard safety: Never auto-connect to live Gmail during automated pytest test execution
        if os.getenv("PYTEST_CURRENT_TEST"):
            self.service = service
            self.inbox_services = inbox_services or {}
        else:
            self.service = service or get_robie_gmail_service()
            self.inbox_services = inbox_services or get_all_active_inbox_services()
        self.outreach_email = settings.gmail_outreach_email
        self.downloads_dir = settings.downloads_path

    def is_authenticated(self) -> bool:
        return self.service is not None

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        attachment_paths: Optional[List[Path]] = None,
        cc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
        applicant_id: Optional[str] = None,
        policy_number: Optional[str] = None,
        discussion_title: Optional[str] = None,
        carrier_name: Optional[str] = None,
        line_of_business: Optional[str] = None,
        save_to_ezlynx: bool = True
    ) -> Dict[str, Any]:
        """Sends an email from Robie (robie@streetsmart.insurance) with threading headers, optional CC, and HTML support."""
        # Hard safety check: Prevent accidental real email sends during testing
        if os.getenv("PYTEST_CURRENT_TEST") or not self.is_authenticated() or "example.com" in to_email.lower():
            cc_info = f" | CC: {', '.join(cc)}" if cc else ""
            logger.info(
                f"[SIMULATION] Gmail API Send (From: {self.outreach_email}) -> To: {to_email}{cc_info} | Subject: '{subject}' | Thread: {thread_id or 'New'}"
            )
            return {
                "status": "simulated",
                "id": f"sim_msg_{to_email.split('@')[0]}",
                "threadId": thread_id or f"sim_thread_{to_email.split('@')[0]}",
                "labelIds": ["SENT"]
            }

        message = MIMEMultipart("alternative" if html_body else "mixed")
        message["to"] = to_email
        message["from"] = self.outreach_email
        message["subject"] = subject
        if cc:
            message["cc"] = ", ".join(cc)

        if in_reply_to and not in_reply_to.startswith("sim_"):
            message["In-Reply-To"] = in_reply_to
        if references and not references.startswith("sim_"):
            message["References"] = references

        message.attach(MIMEText(body_text, "plain"))
        if html_body:
            message.attach(MIMEText(html_body, "html"))

        if attachment_paths:
            for p in attachment_paths:
                if p.exists():
                    with open(p, "rb") as af:
                        part = MIMEApplication(af.read(), Name=p.name)
                        part["Content-Disposition"] = f'attachment; filename="{p.name}"'
                        message.attach(part)

        raw_encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        send_body = {"raw": raw_encoded}
        if thread_id and not thread_id.startswith("sim_"):
            send_body["threadId"] = thread_id

        try:
            sent_msg = self.service.users().messages().send(userId="me", body=send_body).execute()
            logger.info(f"Email successfully sent from {self.outreach_email} to {to_email} (Msg ID: {sent_msg.get('id')})")
            if save_to_ezlynx:
                self._save_sent_email_to_ezlynx(
                    to_email=to_email,
                    subject=subject,
                    body_text=body_text,
                    sent_msg_id=sent_msg.get("id"),
                    applicant_id=applicant_id,
                    policy_number=policy_number,
                    discussion_title=discussion_title,
                    carrier_name=carrier_name,
                    line_of_business=line_of_business,
                    cc_emails=cc
                )
            return sent_msg
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return {"status": "error", "error": str(e)}

    def _save_sent_email_to_ezlynx(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        sent_msg_id: Optional[str] = None,
        applicant_id: Optional[str] = None,
        policy_number: Optional[str] = None,
        discussion_title: Optional[str] = None,
        carrier_name: Optional[str] = None,
        line_of_business: Optional[str] = None,
        cc_emails: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Hard-coded rule: Whenever Robie sends an email from Gmail, save it to the EZLynx client file."""
        try:
            from src.ezlynx.api_client import EZLynxApiClient
            ezlynx = EZLynxApiClient()

            target_app_id = applicant_id
            target_policy_num = policy_number

            if not target_app_id:
                import re
                pol_match = re.search(
                    r'\b(?:[0-9]{2}[A-Z]{3,4}[0-9A-Z]+|[A-Z0-9]{3,4}[0-9]{6,10}|[0-9]{7,10}|6S[0-9A-Z\-]+|13[0-9A-Z\-]+|UBB[0-9]+)\b',
                    f"{subject} {body_text}"
                )
                if pol_match and not target_policy_num:
                    target_policy_num = pol_match.group(0)

                if target_policy_num:
                    try:
                        pol_info = ezlynx.search_policy_by_number(target_policy_num)
                        if pol_info and pol_info.get("applicantId"):
                            target_app_id = str(pol_info.get("applicantId"))
                    except Exception as e:
                        logger.debug(f"Could not lookup policy {target_policy_num} in EZLynx: {e}")

                if not target_app_id and to_email and "@" in to_email and not any(internal in to_email.lower() for internal in ["@streetsmart.insurance"]):
                    try:
                        apps = ezlynx.search_applicants(to_email)
                        if apps and len(apps) > 0:
                            target_app_id = str(apps[0].get("applicantId") or apps[0].get("id"))
                    except Exception as e:
                        logger.debug(f"Could not search applicant by email {to_email}: {e}")

            if not target_app_id:
                logger.info(f"[EZLynx Sync] Outbound email to {to_email} ('{subject}') not linked to an applicant file. Skipping note filing.")
                return None

            title = discussion_title or "Email sent by Robie"

            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            note_lines = [
                f"Policy: #{target_policy_num or 'N/A'} ({line_of_business or 'Commercial'} - {carrier_name or 'Direct'})",
                f"Outbound Email Sent by Robie | Date: {today_str}",
                "",
                "Details:",
                f"- To: {to_email}",
            ]
            if cc_emails:
                note_lines.append(f"- Cc: {', '.join(cc_emails)}")
            note_lines.append(f"- Subject: {subject}")
            if sent_msg_id:
                note_lines.append(f"- Message ID: {sent_msg_id}")
            note_lines.append("")
            note_lines.append("Body:")
            note_lines.append(body_text[:1500] if body_text else "(No text body)")
            note_lines.append("")
            note_lines.append("ROBIE was here")

            formatted_note = "\n".join(note_lines)

            res = ezlynx.add_note_to_discussion(
                applicant_id=target_app_id,
                discussion_title=title,
                note_text=formatted_note,
                policy_number=target_policy_num,
                line_of_business=line_of_business,
                carrier_name=carrier_name,
                honor_explicit_title=True,
                require_existing_discussion=False
            )
            logger.info(f"✅ [EZLynx Sync] Saved outbound email to applicant {target_app_id} discussion '{title}'. Result: {res}")
            return res
        except Exception as e:
            logger.warning(f"⚠️ [EZLynx Sync] Failed to save outbound email to EZLynx: {e}")
            return None

    def poll_matching_replies(
        self,
        tracking_prefix: str = "[RENEWAL-REQ-",
        active_policies: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Polls ALL active connected inboxes (Robie + Hello):
        1. Checks for tracking codes '[RENEWAL-REQ-XXXX]'.
        2. In Hello inbox: Also scans for proactive carrier renewal / loss run emails matching active policies.
        """
        all_replies = []
        active_inboxes = filter_renewal_poll_inboxes(
            self.inbox_services or {"robie@streetsmart.insurance": self.service}
        )

        if not any(active_inboxes.values()):
            logger.info("[SIMULATION] Dual-inbox poll -> No active Gmail credentials, simulation mode.")
            return []

        for inbox_name, svc in active_inboxes.items():
            if not svc:
                continue

            logger.info(f"Polling inbox: {inbox_name} for renewal replies and incoming carrier emails...")
            
            # Query 1: Tracking prefix search
            try:
                res = svc.users().messages().list(userId="me", q=f"in:inbox {tracking_prefix}").execute()
                for m in res.get("messages", []):
                    parsed = self._fetch_and_parse_msg(svc, m["id"], inbox_name)
                    if parsed and not any(r["message_id"] == parsed["message_id"] for r in all_replies):
                        all_replies.append(parsed)
            except Exception as e:
                logger.error(f"Error querying {inbox_name} for tracking code: {e}")

            # Query 2: Proactive Carrier Renewals & Notices in Hello inbox
            if "hello" in inbox_name.lower() and active_policies:
                try:
                    res = svc.users().messages().list(
                        userId="me",
                        q="in:inbox (renewal OR 'loss run' OR 'loss runs' OR quote OR 'upcoming renewal' OR 'non-renewal' OR nonrenewal OR decline)"
                    ).execute()
                    for m in res.get("messages", []):
                        parsed = self._fetch_and_parse_msg(svc, m["id"], inbox_name)
                        if parsed and not any(r["message_id"] == parsed["message_id"] for r in all_replies):
                            # Check if email body or attachment relates to one of our active policies
                            if self._matches_any_policy(parsed, active_policies):
                                all_replies.append(parsed)
                except Exception as e:
                    logger.error(f"Error querying proactive carrier emails in {inbox_name}: {e}")

        return all_replies

    def _fetch_and_parse_msg(self, svc: Resource, msg_id: str, inbox_name: str) -> Optional[Dict[str, Any]]:
        try:
            msg_data = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
            return self._parse_message_payload(svc, msg_data, inbox_name)
        except Exception as e:
            logger.error(f"Error fetching message {msg_id} from {inbox_name}: {e}")
            return None

    def _matches_any_policy(self, parsed_msg: Dict[str, Any], active_policies: List[Dict[str, Any]]) -> bool:
        """Checks if an email matches any active pending policy number or insured name."""
        search_text = f"{parsed_msg.get('subject', '')} {parsed_msg.get('body', '')} {' '.join(a['filename'] for a in parsed_msg.get('attachments', []))}".lower()
        for pol in active_policies:
            numbers = pol.get("policy_numbers") or [pol.get("policy_number", "")]
            number_hit = False
            for raw in numbers:
                pol_num = (raw or "").lower().split()[0]  # strip sub-labels like NTL / APD
                if pol_num and len(pol_num) >= 5 and pol_num in search_text:
                    number_hit = True
                    break
            insured = pol.get("insured_name", "").lower()
            if number_hit or (insured and len(insured) > 4 and insured in search_text):
                # Tag with matched policy ID
                parsed_msg["matched_policy_id"] = pol.get("id")
                logger.info(f"Matched proactive carrier email in {parsed_msg.get('inbox_source')} to Policy #{pol.get('policy_number')} ({pol.get('insured_name')})")
                return True
        return False

    def _parse_message_payload(self, svc: Resource, msg_data: Dict[str, Any], inbox_name: str) -> Optional[Dict[str, Any]]:
        msg_id = msg_data.get("id")
        thread_id = msg_data.get("threadId")
        payload = msg_data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        sender = headers.get("from", "")
        # Don't process our own outbound messages as replies
        if self.outreach_email.lower() in sender.lower() or inbox_name.lower() in sender.lower():
            return None

        subject = headers.get("subject", "")

        # Filter out automatic bounce-backs / out-of-office / server notifications
        sub_lower = subject.strip().lower()
        if any(sub_lower.startswith(prefix) for prefix in [
            "automatic reply:", "auto:", "auto reply:", "out of office:", "undeliverable:",
            "delivery status notification", "failure notice", "undelivered mail"
        ]):
            logger.debug(f"Ignoring automated auto-reply: {subject} from {sender}")
            return None

        # Recursive extraction of text parts and attachments
        body_text = ""
        text_html = ""
        attachments = []

        def _walk_parts(p: Dict[str, Any]):
            nonlocal body_text, text_html
            m_type = p.get("mimeType", "")
            f_name = p.get("filename", "")
            p_body = p.get("body", {})

            if f_name and "attachmentId" in p_body:
                att_id = p_body["attachmentId"]
                saved_path = self._download_attachment(svc, msg_id, att_id, f_name)
                if saved_path:
                    attachments.append({"filename": f_name, "path": saved_path})

            if m_type == "text/plain" and "data" in p_body and not body_text:
                body_text = base64.urlsafe_b64decode(p_body["data"]).decode("utf-8", errors="ignore")
            elif m_type == "text/html" and "data" in p_body and not text_html:
                text_html = base64.urlsafe_b64decode(p_body["data"]).decode("utf-8", errors="ignore")

            for subpart in p.get("parts", []):
                _walk_parts(subpart)

        _walk_parts(payload)

        # Fallback to stripped HTML text if plain text was missing
        if not body_text and text_html:
            import re
            body_text = re.sub(r"<[^>]+>", " ", text_html)

        clean_text = extract_clean_reply_text(body_text)

        return {
            "message_id": msg_id,
            "thread_id": thread_id,
            "inbox_source": inbox_name,
            "sender": sender,
            "subject": subject,
            "body": body_text,
            "clean_reply_text": clean_text,
            "attachments": attachments,
            "date": headers.get("date")
        }

    def _download_attachment(self, svc: Resource, message_id: str, attachment_id: str, filename: str) -> Optional[Path]:
        try:
            att = svc.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()

            file_data = base64.urlsafe_b64decode(att["data"])
            dest_dir = self.downloads_dir / "inbox_attachments"
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_file = dest_dir / f"{message_id}_{filename}"
            with open(out_file, "wb") as f:
                f.write(file_data)
            logger.info(f"Downloaded email attachment: {out_file.name}")
            return out_file
        except Exception as e:
            logger.error(f"Error downloading attachment {filename}: {e}")
            return None

    def mark_message_read(self, message_id: str, inbox_name: Optional[str] = None) -> bool:
        """Removes the UNREAD label from a message in the designated or default inbox."""
        svc = self.inbox_services.get(inbox_name) if inbox_name else self.service
        if not svc:
            svc = self.service
        if not svc:
            if os.getenv("PYTEST_CURRENT_TEST"):
                logger.info(f"[SIMULATION] Mark message {message_id} as read")
                return True
            logger.warning(f"No Gmail service available to mark message {message_id} as read.")
            return False

        try:
            svc.users().messages().modify(
                userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            logger.info(f"Marked message {message_id} as READ.")
            return True
        except Exception as e:
            logger.error(f"Failed to mark message {message_id} as READ: {e}")
            return False

    def batch_mark_read(self, message_ids: List[str], inbox_name: Optional[str] = None) -> int:
        """Batch removes the UNREAD label from a list of message IDs."""
        if not message_ids:
            return 0

        svc = self.inbox_services.get(inbox_name) if inbox_name else self.service
        if not svc:
            svc = self.service
        if not svc:
            if os.getenv("PYTEST_CURRENT_TEST"):
                logger.info(f"[SIMULATION] Batch mark {len(message_ids)} messages as read")
                return len(message_ids)
            logger.warning("No Gmail service available for batch mark read.")
            return 0

        marked = 0
        # Process in chunks of 50 via batchModify if available, or sequential
        try:
            for i in range(0, len(message_ids), 50):
                chunk = message_ids[i:i+50]
                svc.users().messages().batchModify(
                    userId="me",
                    body={"ids": chunk, "removeLabelIds": ["UNREAD"]}
                ).execute()
                marked += len(chunk)
            logger.info(f"Batch marked {marked} messages as READ.")
            return marked
        except Exception as e:
            logger.warning(f"batchModify failed ({e}); falling back to individual modify...")
            for mid in message_ids:
                if self.mark_message_read(mid, inbox_name):
                    marked += 1
            return marked

    def trash_message(self, message_id: str, inbox_name: Optional[str] = None) -> bool:
        """Moves a message to trash."""
        svc = self.inbox_services.get(inbox_name) if inbox_name else self.service
        if not svc:
            svc = self.service
        if not svc:
            if os.getenv("PYTEST_CURRENT_TEST"):
                logger.info(f"[SIMULATION] Trash message {message_id}")
                return True
            logger.warning(f"No Gmail service available to trash message {message_id}.")
            return False

        try:
            svc.users().messages().trash(userId="me", id=message_id).execute()
            logger.info(f"Trashed message {message_id}.")
            return True
        except Exception as e:
            logger.error(f"Failed to trash message {message_id}: {e}")
            return False

