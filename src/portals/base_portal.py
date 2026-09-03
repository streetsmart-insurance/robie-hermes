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
