"""EZLynx Playwright Browser Adapter for UI-based note posting and document upload."""

import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, BrowserContext, Page

from src.config import settings

logger = logging.getLogger("ezlynx_browser")

class EZLynxBrowserAdapter:
    """Automates EZLynx UI actions via Playwright for discussions, notes, and attachments."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        headless: bool = True
    ):
        self.base_url = (base_url or settings.ezlynx_base_url).rstrip("/")
        self.user_data_dir = os.path.expanduser(user_data_dir or settings.ezlynx_user_data_dir)
        self.headless = headless if headless is not None else settings.playwright_headless
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def initialize(self) -> Page:
        """Launches persistent context with Chrome profile."""
        self.playwright = await async_playwright().start()
        Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self.page

    async def add_note_ui(
        self,
        applicant_id: str,
        discussion_title: str,
        note_text: str
    ) -> bool:
        """Navigates to Applicant page in EZLynx and adds note under the discussion title."""
        if not self.page:
            await self.initialize()

        logger.info(f"[EZLynx UI] Navigating to Applicant {applicant_id}...")
        applicant_url = f"{self.base_url}/applicants/{applicant_id}"
        await self.page.goto(applicant_url, wait_until="networkidle")
        await self.page.wait_for_timeout(2000)

        # Click Activity / Discussion tab
        act_tab = await self.page.query_selector("a:has-text('Activity'), button:has-text('Discussions')")
        if act_tab:
            await act_tab.click()
            await self.page.wait_for_timeout(1500)

        # Click Add Note
        add_btn = await self.page.query_selector("button:has-text('Add Note'), button:has-text('New Note'), a.btn-add-note")
        if add_btn:
            await add_btn.click()
            await self.page.wait_for_timeout(1000)

            # Fill title & body
            title_input = await self.page.query_selector("input[placeholder*='Title'], input#txtTitle, select#discussionType")
            if title_input:
                await title_input.fill(discussion_title)

            body_input = await self.page.query_selector("textarea[placeholder*='note'], div[contenteditable='true']")
            if body_input:
                await body_input.fill(note_text)

            # Save note
            save_btn = await self.page.query_selector("button:has-text('Save'), button:has-text('Post')")
            if save_btn:
                await save_btn.click()
                await self.page.wait_for_timeout(1500)
                logger.info(f"[EZLynx UI] Note added under '{discussion_title}' for Applicant {applicant_id}")
                return True

        logger.warning(f"[EZLynx UI] Could not find note form elements on page for {applicant_id}")
        return False

    async def close(self):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
