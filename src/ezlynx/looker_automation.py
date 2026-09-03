"""EZLynx Looker Report Automation & CDP Connection Helper."""

import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

logger = logging.getLogger("ezlynx_looker")

class EZLynxLookerController:
    """Connects to user's Chrome session via CDP or launches browser to manage Looker reports."""

    def __init__(self, cdp_url: str = "http://localhost:9222"):
        self.cdp_url = cdp_url
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def connect_cdp(self) -> Page:
        """Connects over Chrome DevTools Protocol to your existing open Chrome window."""
        self.playwright = await async_playwright().start()
        logger.info(f"Connecting to Chrome on {self.cdp_url}...")
        self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
        
        # Check existing tabs
        contexts = self.browser.contexts
        if contexts and contexts[0].pages:
            self.context = contexts[0]
            # Search for EZLynx tab
            for p in self.context.pages:
                if "ezlynx.com" in p.url or "looker" in p.url:
                    self.page = p
                    logger.info(f"Found active EZLynx tab: {p.url}")
                    return self.page
            self.page = self.context.pages[0]
        else:
            self.context = await self.browser.new_context()
            self.page = await self.context.new_page()

        return self.page

    async def configure_manual_renewal_filter(self, report_url: str = "https://app.ezlynx.com/web/looker-reports/report/3471?isDashboard=false") -> Dict[str, Any]:
        """Navigates to or attaches to Looker Report and configures the Manual filter."""
        if not self.page:
            await self.connect_cdp()

        if self.page.url != report_url:
            logger.info(f"Navigating to {report_url}...")
            await self.page.goto(report_url, wait_until="networkidle")
            await self.page.wait_for_timeout(3000)

        # Wait for Looker frame or embedded report
        logger.info("Locating Looker reporting frame...")
        frames = self.page.frames
        target_frame = None
        for frame in frames:
            if "looker" in frame.url or "embed" in frame.url:
                target_frame = frame
                break

        ctx = target_frame if target_frame else self.page

        # 1. Expand Filters if collapsed
        filter_toggle = await ctx.query_selector("button:has-text('Filters'), div[aria-label*='Filter'], .filter-toggle")
        if filter_toggle:
            await filter_toggle.click()
            await self.page.wait_for_timeout(1000)

        # 2. Look for Source / Download filter elements
        logger.info("Applying 'Manual' filter...")
        source_inputs = await ctx.query_selector_all("input[aria-label*='Source'], input[placeholder*='Source'], select[name*='source'], [role='combobox']")
        
        applied = False
        for inp in source_inputs:
            try:
                await inp.click()
                await inp.fill("Manual")
                await self.page.keyboard.press("Enter")
                applied = True
                logger.info("Successfully populated Source filter with 'Manual'")
                break
            except Exception:
                continue

        # 3. Click Run Button
        run_btn = await ctx.query_selector("button:has-text('Run'), button[aria-label='Run'], .run-button")
        if run_btn:
            await run_btn.click()
            logger.info("Clicked Run report button.")
            await self.page.wait_for_timeout(3000)

        # Capture screenshot for confirmation
        screenshot_path = Path("data/downloads/looker_manual_filtered.png")
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(screenshot_path))
        logger.info(f"Saved confirmation screenshot to {screenshot_path}")

        return {
            "status": "success",
            "filter_applied": applied,
            "screenshot": str(screenshot_path)
        }

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
