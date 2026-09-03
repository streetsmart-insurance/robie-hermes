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

class GmailRenewalClient:
    """Manages email sending from Robie, and reply polling across Robie and Hello inboxes."""

    def __init__(
        self,
        service: Optional[Resource] = None,
        inbox_services: Optional[Dict[str, Resource]] = None
    ):
        self.service = service or get_robie_gmail_service()
        self.outreach_email = settings.gmail_outreach_email
        self.downloads_dir = settings.downloads_path
        self.inbox_services = inbox_services or get_all_active_inbox_services()

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
        cc: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Sends an email from Robie (robie@streetsmart.insurance) with threading headers and optional CC."""
        if not self.is_authenticated():
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

        message = MIMEMultipart()
        message["to"] = to_email
        message["from"] = self.outreach_email
        message["subject"] = subject
        if cc:
            message["cc"] = ", ".join(cc)

        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references

        message.attach(MIMEText(body_text, "plain"))

        if attachment_paths:
            for p in attachment_paths:
                if p.exists():
                    with open(p, "rb") as af:
                        part = MIMEApplication(af.read(), Name=p.name)
                        part["Content-Disposition"] = f'attachment; filename="{p.name}"'
                        message.attach(part)

        raw_encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        send_body = {"raw": raw_encoded}
        if thread_id:
            send_body["threadId"] = thread_id

        try:
            sent_msg = self.service.users().messages().send(userId="me", body=send_body).execute()
            logger.info(f"Email successfully sent from {self.outreach_email} to {to_email} (Msg ID: {sent_msg.get('id')})")
            return sent_msg
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return {"status": "error", "error": str(e)}

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
        active_inboxes = self.inbox_services or {"robie@streetsmart.insurance": self.service}

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

            # Query 2: Proactive Carrier Renewals in Hello inbox
            if "hello" in inbox_name.lower() and active_policies:
                try:
                    res = svc.users().messages().list(
                        userId="me",
                        q="in:inbox (renewal OR 'loss run' OR 'loss runs' OR quote OR 'upcoming renewal') has:attachment"
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
            pol_num = pol.get("policy_number", "").lower()
            insured = pol.get("insured_name", "").lower()
            if (pol_num and pol_num in search_text) or (insured and len(insured) > 4 and insured in search_text):
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
        body_text = ""
        attachments = []

        parts = payload.get("parts", [payload])
        for part in parts:
            mime_type = part.get("mimeType", "")
            filename = part.get("filename", "")
            body = part.get("body", {})

            if mime_type == "text/plain" and "data" in body:
                body_text += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")
            elif mime_type == "text/html" and not body_text and "data" in body:
                body_text += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="ignore")

            if filename and "attachmentId" in body:
                att_id = body["attachmentId"]
                saved_path = self._download_attachment(svc, msg_id, att_id, filename)
                if saved_path:
                    attachments.append({"filename": filename, "path": saved_path})

        return {
            "message_id": msg_id,
            "thread_id": thread_id,
            "inbox_source": inbox_name,
            "sender": sender,
            "subject": subject,
            "body": body_text,
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
