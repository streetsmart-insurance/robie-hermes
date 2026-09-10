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

        logger.info(f"[EZLynx UI] Navigating to Applicant {applicant_id} Activity...")
        applicant_url = f"{self.base_url}/web/account/{applicant_id}/activity"
        await self.page.goto(applicant_url, wait_until="networkidle")
        await self.page.wait_for_timeout(3000)

        # 1. Search for existing discussion/task (e.g. 'Commercial Auto Renewal')
        search_terms = [discussion_title, "Commercial Auto Renewal", "Manual Commercial Renewal"]
        
        # Look for wrench button in discussion card matching title
        added = await self.page.evaluate("""(terms) => {
            const divs = Array.from(document.querySelectorAll('.activity-div, .activity-item, mat-card'));
            for (const term of terms) {
                const target = divs.find(el => el.textContent.toLowerCase().includes(term.toLowerCase()));
                if (target) {
                    const wrench = target.querySelector('button[title*="Edit this task"], button[title*="Edit"], button:has(mat-icon:has-text("build"))');
                    if (wrench) {
                        wrench.click();
                        return true;
                    }
                }
            }
            return false;
        }""", search_terms)

        if added:
            await self.page.wait_for_timeout(1500)
            # Find textarea in drawer
            note_area = await self.page.query_selector("textarea.note, textarea[placeholder*='Task note'], textarea[placeholder*='note']")
            if note_area:
                await note_area.fill(note_text)
                await self.page.wait_for_timeout(500)
                # Click Save button in drawer
                save_btn = await self.page.query_selector("button[type='submit']:has-text('Save'), button.mat-primary[type='submit']")
                if save_btn:
                    await save_btn.click()
                    await self.page.wait_for_timeout(2000)
                    logger.info(f"[EZLynx UI] Note added via wrench icon for Applicant {applicant_id}")
                    return True

        # Fallback: Top 'Add Note' or 'Add new note' button
        fallback_btn = await self.page.query_selector("button:has-text('Add new note'), button:has-text('Add Note')")
        if fallback_btn:
            await fallback_btn.click()
            await self.page.wait_for_timeout(1500)
            note_area = await self.page.query_selector("textarea[placeholder*='note'], textarea.note")
            if note_area:
                await note_area.fill(note_text)
                save_btn = await self.page.query_selector("button:has-text('Save'), button[type='submit']")
                if save_btn:
                    await save_btn.click()
                    await self.page.wait_for_timeout(2000)
                    logger.info(f"[EZLynx UI] Note added via global drawer for Applicant {applicant_id}")
                    return True

        logger.warning(f"[EZLynx UI] Could not find note form elements on page for {applicant_id}")
        return False

    async def close(self):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
