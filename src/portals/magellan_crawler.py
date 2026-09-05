"""Magellan AI Phone & Conversational Intelligence Crawler & Auditor.

Ingests real customer sentiment (Satisfied, Neutral, Sad/Frustrated),
intent tags (At-Risk, Cancellation, Claims, Billing, COI, Quotes),
and call transcripts directly from Magellan (https://app.magellan.insure).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.config import settings
from src.security.secrets_manager import secrets_mgr
from src.portals.carrier_agents import handle_portal_2fa_if_present

logger = logging.getLogger("magellan_crawler")


@dataclass
class MagellanCallRecord:
    """Represents a call analyzed by Magellan AI."""
    date_time: datetime
    from_phone: str
    to_phone: str
    duration_seconds: int
    sentiment: str  # "Satisfied", "Neutral", "Sad", "Frustrated"
    tags: List[str] = field(default_factory=list)
    is_handled: bool = True
    caller_name: Optional[str] = None
    transcript_summary: Optional[str] = None
    at_risk_flag: bool = False
    cancellation_flag: bool = False


class MagellanAuditor:
    """Analyzes Magellan sentiment metrics and correlates them with renewal candidates."""

    @staticmethod
    def classify_sentiment(raw_sentiment: str) -> str:
        s = (raw_sentiment or "").strip().lower()
        if any(w in s for w in ["sad", "frustrat", "angry", "negative"]):
            return "SAD_FRUSTRATED"
        elif "neutral" in s:
            return "NEUTRAL"
        elif any(w in s for w in ["satisf", "positive", "happy"]):
            return "SATISFIED"
        return "UNKNOWN"

    @staticmethod
    def is_churn_risk(record: MagellanCallRecord) -> bool:
        """Determines if a call indicates churn or dissatisfaction."""
        if record.at_risk_flag or record.cancellation_flag:
            return True
        sentiment = MagellanAuditor.classify_sentiment(record.sentiment)
        if sentiment == "SAD_FRUSTRATED":
            return True
        tag_lower = [t.lower() for t in record.tags]
        if any(k in tag_lower for k in ["cancellation", "at-risk", "at-risk customer", "cancel", "rate increase"]):
            return True
        return False

    @staticmethod
    def correlate_renewal(
        insured_name: str,
        insured_phone: Optional[str],
        magellan_records: List[MagellanCallRecord],
    ) -> Optional[Dict[str, Any]]:
        """Matches a renewing insured against Magellan call history to extract sentiment and flags."""
        clean_phone = "".join(c for c in str(insured_phone or "") if c.isdigit())[-10:] if insured_phone else ""
        norm_name = insured_name.strip().lower() if insured_name else ""

        matched: List[MagellanCallRecord] = []
        for rec in magellan_records:
            rec_phone = "".join(c for c in rec.from_phone if c.isdigit())[-10:]
            rec_caller = (rec.caller_name or "").strip().lower()

            if clean_phone and clean_phone == rec_phone:
                matched.append(rec)
            elif norm_name and norm_name in rec_caller:
                matched.append(rec)

        if not matched:
            return None

        # Sort newest first
        matched.sort(key=lambda r: r.date_time, reverse=True)
        latest = matched[0]
        has_at_risk = any(MagellanAuditor.is_churn_risk(r) for r in matched)

        all_tags = []
        for r in matched:
            for t in r.tags:
                if t not in all_tags:
                    all_tags.append(t)

        return {
            "has_recent_call": True,
            "latest_call_date": latest.date_time.isoformat(),
            "latest_sentiment": latest.sentiment,
            "classified_sentiment": MagellanAuditor.classify_sentiment(latest.sentiment),
            "at_risk_churn": has_at_risk,
            "cancellation_intent": any(r.cancellation_flag or "cancellation" in [t.lower() for t in r.tags] for r in matched),
            "all_tags": all_tags,
            "summary": latest.transcript_summary,
            "total_calls": len(matched),
        }


class MagellanCrawler:
    """Automates login and intelligence extraction from Magellan (https://app.magellan.insure)."""

    def __init__(
        self,
        cdp_url: Optional[str] = None,
        base_url: str = "https://app.magellan.insure",
        headless: bool = True,
    ):
        self.cdp_url = cdp_url or os.getenv("MAGELLAN_CDP_URL") or os.getenv("EZLYNX_CDP_ENDPOINT") or "http://127.0.0.1:9222"
        self.base_url = base_url.rstrip("/")
        self.login_url = f"{self.base_url}/login"
        self.headless = headless

    def get_credentials(self) -> Dict[str, Optional[str]]:
        """Resolves username and password for Magellan from Secret Manager or environment."""
        username = (
            secrets_mgr.get_credential("magellan", "username")
            or os.getenv("MAGELLAN_USERNAME")
            or secrets_mgr.get_credential("ezlynx", "username")
        )
        password = (
            secrets_mgr.get_credential("magellan", "password")
            or os.getenv("MAGELLAN_PASSWORD")
        )
        return {"username": username, "password": password}

    async def is_authenticated(self, page: Page) -> bool:
        """Checks if the current page has an active Magellan session."""
        try:
            curr_url = page.url
            if "/login" not in curr_url and "magellan.insure" in curr_url:
                # Check for dashboard indicators
                body = await page.inner_text("body")
                if any(w in body.lower() for w in ["calls", "dashboard", "analytics", "transcripts", "sentiment"]):
                    return True
            return False
        except Exception:
            return False

    async def login(self, page: Page) -> bool:
        """Performs automated login at https://app.magellan.insure/login."""
        creds = self.get_credentials()
        username = creds["username"]
        password = creds["password"]

        if not username or not password:
            logger.warning("[Magellan] No credentials available in Secret Manager or env.")
            return False

        logger.info(f"[Magellan] Navigating to {self.login_url}...")
        await page.goto(self.login_url, wait_until="networkidle", timeout=30000)

        if await self.is_authenticated(page):
            logger.info("[Magellan] Session is already authenticated.")
            return True

        # Locate Email input
        email_selector = "input#email, input[type='email'], input[placeholder*='email' i], input[type='text']"
        email_input = await page.wait_for_selector(email_selector, timeout=10000)
        if not email_input:
            logger.error("[Magellan] Could not locate email input field.")
            return False

        await email_input.fill(username)
        await asyncio.sleep(0.3)

        # Locate Password input
        pwd_selector = "input#password, input[type='password'], input[placeholder*='password' i]"
        pwd_input = await page.wait_for_selector(pwd_selector, timeout=10000)
        if not pwd_input:
            logger.error("[Magellan] Could not locate password input field.")
            return False

        await pwd_input.fill(password)
        await asyncio.sleep(0.3)

        # Submit login
        submit_btn = await page.query_selector("button[type='submit'], button:has-text('Login'), button:has-text('Log In')")
        if not submit_btn:
            logger.error("[Magellan] Could not locate Login submit button.")
            return False

        logger.info("[Magellan] Submitting credentials...")
        await submit_btn.click()
        await page.wait_for_timeout(4000)

        # Handle 2FA if present
        await handle_portal_2fa_if_present(page, "Magellan")

        # Verify post-login URL or dashboard element
        await page.wait_for_load_state("networkidle", timeout=15000)
        if "/login" not in page.url:
            logger.info(f"[Magellan] Successfully logged in! Landed on: {page.url}")
            return True
        else:
            logger.warning(f"[Magellan] Login did not navigate away from login page. Current URL: {page.url}")
            return False

    async def fetch_recent_calls(self, page: Page, limit: int = 50) -> List[MagellanCallRecord]:
        """Extracts recent call sentiment and transcript records from Magellan."""
        records: List[MagellanCallRecord] = []
        try:
            # First attempt: Try internal API fetch from within authenticated context
            api_data = await page.evaluate('''async (lim) => {
                const endpoints = ['/api/calls', '/api/v1/calls', '/api/records'];
                for (const ep of endpoints) {
                    try {
                        const r = await fetch(ep + '?limit=' + lim);
                        if (r.ok) return await r.json();
                    } catch (e) {}
                }
                return null;
            }''', limit)

            if api_data and isinstance(api_data, list):
                for item in api_data:
                    dt = datetime.now(timezone.utc)
                    if "date" in item or "createdAt" in item:
                        raw_date = item.get("date") or item.get("createdAt")
                        try:
                            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        except Exception:
                            pass
                    records.append(
                        MagellanCallRecord(
                            date_time=dt,
                            from_phone=item.get("fromPhone") or item.get("from") or "",
                            to_phone=item.get("toPhone") or item.get("to") or "",
                            duration_seconds=int(item.get("duration") or item.get("durationSeconds") or 0),
                            sentiment=item.get("sentiment") or "Neutral",
                            tags=item.get("tags") or [],
                            is_handled=bool(item.get("isHandled", True)),
                            caller_name=item.get("callerName") or item.get("customerName"),
                            transcript_summary=item.get("summary") or item.get("transcriptSummary"),
                            at_risk_flag=bool(item.get("atRisk", False)),
                            cancellation_flag=bool(item.get("cancellation", False)),
                        )
                    )
                logger.info(f"[Magellan] Fetched {len(records)} calls via internal API.")
                return records

        except Exception as e:
            logger.debug(f"[Magellan] API fetch attempt error: {e}")

        logger.info(f"[Magellan] Returning {len(records)} call records.")
        return records
