"""Multi-Inbox 2FA / OTP Interceptor for StreetSmart Carrier Portals.

Monitors carlo@streetsmart.insurance, robie@streetsmart.insurance, and hello@streetsmart.insurance
via Gmail API Domain-Wide Delegation to intercept verification codes, OTPs, and password reset links.
"""

import re
import time
import base64
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from googleapiclient.discovery import build, Resource

from src.email_outreach.auth_setup import get_service_account_credentials

logger = logging.getLogger("otp_interceptor")

DEFAULT_INBOXES = [
    "carlo@streetsmart.insurance",
    "robie@streetsmart.insurance",
    "hello@streetsmart.insurance",
]

@dataclass
class OTPResult:
    code: Optional[str]
    link: Optional[str]
    inbox: str
    sender: str
    subject: str
    message_id: str
    body_snippet: str

class MultiInboxOTPInterceptor:
    """Intercepts OTP codes and activation links across StreetSmart domain inboxes."""

    def __init__(self, inboxes: Optional[List[str]] = None):
        self.inboxes = inboxes or DEFAULT_INBOXES
        self._services: Dict[str, Resource] = {}

    def get_service(self, inbox: str) -> Optional[Resource]:
        """Returns or builds cached Gmail service for a given domain user."""
        if inbox in self._services:
            return self._services[inbox]

        creds = get_service_account_credentials(inbox)
        if creds:
            try:
                service = build("gmail", "v1", credentials=creds)
                self._services[inbox] = service
                return service
            except Exception as e:
                logger.error(f"Failed to build Gmail service for {inbox}: {e}")
        return None

    @staticmethod
    def extract_body(payload: dict) -> str:
        """Extracts text content from a Gmail payload."""
        text = ""
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")

        if body_data:
            try:
                text += base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            except Exception:
                pass

        if "parts" in payload:
            for part in payload["parts"]:
                text += MultiInboxOTPInterceptor.extract_body(part)

        return text

    @staticmethod
    def parse_otp_and_link(text: str, subject: str = "") -> Tuple[Optional[str], Optional[str]]:
        """Parses verification code and/or magic link from email text."""
        combined = f"{subject}\n{text}"
        code = None
        link = None

        # 1. Common OTP Patterns
        # Strip URLs from text before searching for standalone OTP codes
        text_clean = re.sub(r'https?://\S+', ' ', combined)
        # Strip CSS and HTML tags so hex colors like #222222 or tag attributes don't match as OTPs
        text_clean = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', text_clean, flags=re.IGNORECASE)
        text_clean = re.sub(r'<[^<]+?>', ' ', text_clean)
        # Normalize whitespace and HTML entities
        text_clean = re.sub(r'&nbsp;|&#8217;|&#169;|&reg;', ' ', text_clean)

        # 1. Common OTP Patterns
        otp_patterns = [
            r"(?:code is valid for \d+ minutes:?\s*)([0-9]{4,8})\b",
            r"(?:verification code|security code|passcode|one-time code|pin|otp)\s*(?:is|:)\s*[:#]?\s*([0-9A-Za-z]{4,8})\b",
            r"(?:code is|code:)\s*([0-9A-Za-z]{4,8})\b",
            r"(?:temporary code|verify your new device|device.*?code).*?([0-9]{6})\b",
            r"\b([0-9]{6})\b",  # Standard 6-digit OTP
            r"\b([0-9]{4})\b",  # 4-digit PIN
        ]

        for pat in otp_patterns:
            m = re.search(pat, text_clean, re.IGNORECASE)
            if m:
                found = m.group(1).strip()
                if found not in ["2024", "2025", "2026", "2027"]:
                    code = found
                    break

        # 2. Activation or Reset Link Patterns
        link_patterns = [
            r'https?://[^\s"\'<>]+policylink\.neptuneflood\.com[^\s"\'<>]*',
            r'https?://policylink\.neptuneflood\.com[^\s"\'<>]*',
            r'https?://clerk\.[^\s"\'<>]*',
            r'https?://[^\s"\'<>]+(?:verify|confirm|reset|magic|activate|token|invit|setup|callback|ls/click)[^\s"\'<>]*',
        ]

        for pat in link_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                link = m.group(0).strip()
                link = re.sub(r'[\.,;\)\]]+$', '', link)
                break

        return code, link

    def check_inbox_since(
        self,
        inbox: str,
        query: str,
        since_timestamp: int
    ) -> Optional[OTPResult]:
        """Checks a specific inbox for messages matching query since a timestamp."""
        svc = self.get_service(inbox)
        if not svc:
            return None

        full_query = f"{query} after:{since_timestamp}"
        try:
            res = svc.users().messages().list(userId="me", q=full_query, maxResults=5).execute()
            messages = res.get("messages", [])
            for m in messages:
                msg_id = m["id"]
                msg_data = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
                payload = msg_data.get("payload", {})
                headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

                subject = headers.get("Subject", "")
                sender = headers.get("From", "")
                body = self.extract_body(payload) or msg_data.get("snippet", "")

                code, link = self.parse_otp_and_link(body, subject)
                if code or link:
                    return OTPResult(
                        code=code,
                        link=link,
                        inbox=inbox,
                        sender=sender,
                        subject=subject,
                        message_id=msg_id,
                        body_snippet=body[:200]
                    )
        except Exception as e:
            logger.debug(f"Error querying {inbox} with query '{full_query}': {e}")
        return None

    def wait_for_otp(
        self,
        query: str = "verification OR code OR reset OR password OR activate",
        max_wait_seconds: int = 60,
        poll_interval: int = 3,
        since_offset_seconds: int = 120
    ) -> Optional[OTPResult]:
        """Polls all configured inboxes concurrently until an OTP or link is found."""
        start_time = int(time.time())
        since_timestamp = start_time - since_offset_seconds
        deadline = time.time() + max_wait_seconds

        logger.info(f"Polling inboxes {self.inboxes} for OTP matching '{query}'...")

        while time.time() < deadline:
            for inbox in self.inboxes:
                result = self.check_inbox_since(inbox, query, since_timestamp)
                if result:
                    logger.info(f"Found OTP/Link in {inbox}: Code={result.code}, Link={result.link}")
                    return result
            time.sleep(poll_interval)

        logger.warning(f"Timeout waiting for OTP after {max_wait_seconds}s.")
        return None

# Singleton instance
otp_interceptor = MultiInboxOTPInterceptor()
