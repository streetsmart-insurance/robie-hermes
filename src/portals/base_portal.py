"""Base interface and runner for Carrier Portal Playwright crawlers."""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from pydantic import BaseModel

class PortalSearchResult(BaseModel):
    success: bool
    policy_found: bool
    renewal_ready: bool
    document_path: Optional[Path] = None
    extracted_premium: Optional[float] = None
    status_message: str

class BaseCarrierPortalCrawler(ABC):
    """Abstract base class for carrier portal scraping bots."""

    def __init__(self, carrier_name: str, base_url: str, headless: bool = True):
        self.carrier_name = carrier_name
        self.base_url = base_url
        self.headless = headless

    async def get_stealth_context(self, playwright):
        """Creates a hardened, persistent stealth context for this carrier."""
        from src.portals.stealth_browser import create_stealth_carrier_context
        return await create_stealth_carrier_context(
            playwright=playwright,
            carrier_key=self.carrier_name,
            headless=self.headless
        )

    async def anti_bot_jitter(self, min_sec: float = 1.2, max_sec: float = 3.0, action: str = "") -> float:
        """Applies randomized natural jitter delay to avoid carrier WAF & bot rate-limiting."""
        from src.portals.stealth_browser import human_delay
        return await human_delay(min_sec, max_sec, label=f"[{self.carrier_name}] {action}" if action else self.carrier_name)

    async def human_type(self, page, selector_or_locator, text: str):
        """Types text with humanized keystroke latency."""
        from src.portals.stealth_browser import human_type
        await human_type(page, selector_or_locator, text)

    async def human_click(self, page, selector_or_locator):
        """Clicks an element with realistic cursor movement."""
        from src.portals.stealth_browser import human_click
        await human_click(page, selector_or_locator)

    async def check_waf_challenge(self, page) -> Tuple[bool, str]:
        """Detects WAF challenge (Cloudflare/DataDome/Akamai) and takes a diagnostic screenshot if found."""
        from src.portals.stealth_browser import detect_waf_challenge, capture_waf_diagnostic, CarrierCircuitBreaker
        is_challenge, challenge_type = await detect_waf_challenge(page)
        if is_challenge:
            await capture_waf_diagnostic(page, self.carrier_name)
            CarrierCircuitBreaker.record_block(self.carrier_name, challenge_type)
        return is_challenge, challenge_type

    @abstractmethod
    async def check_renewal_quote(
        self,
        policy_number: str,
        insured_name: str,
        download_dir: Path
    ) -> PortalSearchResult:
        """Logs into carrier portal, looks up policy, and checks/downloads renewal terms."""
        pass

class GenericPlaywrightCarrierCrawler(BaseCarrierPortalCrawler):
    """Generic Playwright Crawler for unspecified carrier portals."""

    async def check_renewal_quote(
        self,
        policy_number: str,
        insured_name: str,
        download_dir: Path
    ) -> PortalSearchResult:
        return PortalSearchResult(
            success=True,
            policy_found=True,
            renewal_ready=False,
            status_message=f"Portal crawler for {self.carrier_name} is configured ({self.base_url})."
        )
