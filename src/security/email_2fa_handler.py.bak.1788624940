"""Automated 2FA Verification Code Resolver via Email (Gmail API / IMAP).

Polls connected Gmail inboxes for incoming carrier portal / EZLynx security verification codes,
extracts the numeric OTP, and injects it into Playwright login flows.
"""

import re
import time
import base64
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from src.config import settings
from src.email_outreach.auth_setup import get_all_active_inbox_services

logger = logging.getLogger("email_2fa_resolver")

class Email2FACodeResolver:
    """Listens for and extracts multi-factor authentication codes from email."""

    OTP_PATTERNS = [
        r"(?:verification|security|confirmation|passcode|one-time|login)\s*(?:code|passcode)?\s*(?:is|:)?\s*([0-9]{4,8})\b",
        r"\b([0-9]{6})\b",
        r"\b([0-9]{4})\b",
        r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6}\b"
    ]

    def __init__(self, inbox_services: Optional[Dict[str, Any]] = None):
        self.inbox_services = inbox_services or get_all_active_inbox_services()

    async def wait_for_code(
        self,
        service_name: str,
        timeout_sec: int = 90,
        poll_interval_sec: int = 3
    ) -> Optional[str]:
        """Polls connected inboxes every few seconds until a 2FA verification code is found."""
        logger.info(f"[2FA Resolver] Waiting for 2FA email from '{service_name}' (timeout: {timeout_sec}s)...")
        try:
            from src.email_outreach.otp_interceptor import otp_interceptor
            query = f"{service_name} (code OR verification OR security OR passcode OR 'one-time' OR OTP)"
            res = otp_interceptor.wait_for_otp(
                query=query,
                max_wait_seconds=timeout_sec,
                poll_interval=poll_interval_sec
            )
            if res and res.code:
                logger.info(f"[2FA Resolver] ✅ Successfully extracted 2FA code '{res.code}' from '{res.inbox}' for '{service_name}'!")
                return res.code
        except Exception as e:
            logger.warning(f"[2FA Resolver] Error in otp_interceptor fallback: {e}")

        # Fallback to local check
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            code = self.check_latest_2fa_code(service_name)
            if code:
                logger.info(f"[2FA Resolver] ✅ Successfully extracted 2FA code '{code}' for '{service_name}'!")
                return code
            time.sleep(poll_interval_sec)

        logger.warning(f"[2FA Resolver] ⚠️ Timed out waiting for 2FA code for '{service_name}'")
        return None

    def check_latest_2fa_code(self, service_name: str) -> Optional[str]:
        """Checks across all active inbox services for recent 2FA messages."""
        if not self.inbox_services:
            self.inbox_services = get_all_active_inbox_services()

        if not self.inbox_services:
            logger.debug("[2FA Resolver] No active Gmail inbox services available for polling.")
            return None

        # Build query for recent verification messages
        keywords = f"{service_name} (code OR verification OR security OR passcode OR 'one-time')"
        query = f"{keywords} newer_than:10m"

        for inbox_name, svc in self.inbox_services.items():
            if not svc:
                continue
            try:
                results = svc.users().messages().list(userId="me", q=query, maxResults=5).execute()
                messages = results.get("messages", [])

                for msg_item in messages:
                    msg = svc.users().messages().get(userId="me", id=msg_item["id"], format="full").execute()
                    snippet = msg.get("snippet", "")
                    body_text = self._extract_body(msg) or snippet

                    code = self.extract_otp(f"{snippet} {body_text}")
                    if code:
                        return code
            except Exception as e:
                logger.error(f"[2FA Resolver] Error checking inbox '{inbox_name}': {e}")

        return None

    def extract_otp(self, text: str) -> Optional[str]:
        """Extracts OTP code using regular expression patterns."""
        if not text:
            return None

        for pattern in self.OTP_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                code = match.group(1).strip()
                # Exclude common false positives like years (2026, 2025) or standard policy prefixes
                if code not in ("2024", "2025", "2026", "2027"):
                    return code
        return None

    def _extract_body(self, message: Dict[str, Any]) -> str:
        """Decodes email payload body text."""
        payload = message.get("payload", {})
        parts = payload.get("parts", [])
        if not parts and "body" in payload and "data" in payload["body"]:
            try:
                return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
            except Exception:
                return ""

        body_parts = []
        for part in parts:
            if part.get("mimeType") == "text/plain" and "data" in part.get("body", {}):
                try:
                    text = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                    body_parts.append(text)
                except Exception:
                    pass
        return " ".join(body_parts)

email_2fa_resolver = Email2FACodeResolver()
