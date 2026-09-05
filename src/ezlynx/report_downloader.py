"""Automated headless EZLynx report downloader using Robie's credentials."""

import os
import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Page, BrowserContext, Download

from src.config import settings

logger = logging.getLogger("ezlynx_report_downloader")

class EZLynxReportDownloader:
    """Logs into EZLynx using Robie's credentials and exports the 50-day manual renewal report."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        output_dir: Optional[Path] = None,
        headless: bool = True
    ):
        self.username = username or settings.ezlynx_username
        self.password = password or settings.ezlynx_password
        self.base_url = settings.ezlynx_base_url.rstrip("/")
        self.output_dir = output_dir or Path(settings.input_reports_dir)
        self.headless = headless

    async def download_policy_expiration_report(
        self,
        reference_date: Optional[date] = None,
        days_ahead: int = 50
    ) -> Optional[Path]:
        """
        Automates BOB_PolicyExpiration_Detail (Report 28):
        - Logs in with Robie's account
        - Sets Expiration Start = Today, End = Today + 50 Days
        - Ensures 'Source' column is checked
        - Downloads CSV into data/input_reports/
        """
        if not (self.username and self.password):
            logger.info("EZLynx credentials not provided in .env - skipping automated UI download.")
            return None

        ref_date = reference_date or date.today()
        start_date = ref_date + timedelta(days=settings.renewal_window_min_days)
        end_date = ref_date + timedelta(days=settings.renewal_window_max_days)

        start_str = start_date.strftime("%m/%d/%Y")
        end_str = end_date.strftime("%m/%d/%Y")

        report_url = f"{self.base_url}/EZLynxReportPortal/reportwrapper.aspx?Report=BOB_PolicyExpiration_Detail&id=28"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = ref_date.strftime("%Y%m%d")
        dest_file = self.output_dir / f"EZLynx_Manual_Renewals_{timestamp_str}.csv"

        logger.info(f"[Robie Exporter] Launching headless browser for EZLynx export ({start_str} to {end_str})...")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            context = await browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            # 1. Login
            logger.info("[Robie Exporter] Navigating to EZLynx login...")
            await page.goto(self.base_url, wait_until="networkidle")
            
            user_input = await page.query_selector("input#username, input#txtUsername, input[name='username']")
            if user_input:
                logger.info(f"[Robie Exporter] Logging in as {self.username}...")
                await user_input.fill(self.username)
                await page.fill("input#password, input#txtPassword, input[name='password']", self.password)
                await page.click("button[type='submit'], input#btnLogin, button#btnLogin")
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2000)

            # 2. Navigate to Report 28
            logger.info(f"[Robie Exporter] Navigating to Report 28: {report_url}...")
            await page.goto(report_url, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # 3. Fill Date Inputs
            logger.info(f"[Robie Exporter] Setting expiration window: {start_str} to {end_str}...")
            start_inputs = await page.query_selector_all("input[id*='txtStartDate'], input[name*='StartDate'], input[aria-label*='Start']")
            for inp in start_inputs:
                try:
                    await inp.fill(start_str)
                except Exception:
                    pass

            end_inputs = await page.query_selector_all("input[id*='txtEndDate'], input[name*='EndDate'], input[aria-label*='End']")
            for inp in end_inputs:
                try:
                    await inp.fill(end_str)
                except Exception:
                    pass

            # 3b. Ensure Current Policy Status is filtered to 'Active' only
            logger.info("[Robie Exporter] Setting Current Policy Status to 'Active' only...")
            status_dropdown = await page.query_selector("div:has-text('Current Policy Status'), select#ddlCurrentPolicyStatus, div[id*='CurrentPolicyStatus']")
            if status_dropdown:
                try:
                    await status_dropdown.click()
                    await page.wait_for_timeout(500)
                    for val in ["Inactive", "Unknown"]:
                        chk = await page.query_selector(f"input[type='checkbox'][value*='{val}'], label:has-text('{val}') input")
                        if chk and await chk.is_checked():
                            await chk.uncheck()
                    active_chk = await page.query_selector("input[type='checkbox'][value*='Active'], label:has-text('Active') input")
                    if active_chk and not await active_chk.is_checked():
                        await active_chk.check()
                    await page.keyboard.press("Escape")
                except Exception as e:
                    logger.debug(f"Could not adjust policy status dropdown: {e}")

            # 4. Ensure Source Column is Enabled in Manage Columns to focus ONLY on Manual
            logger.info("[Robie Exporter] Ensuring 'Source' column is selected in Manage Columns...")
            col_picker = await page.query_selector("button:has-text('Manage Columns'), div:has-text('Manage Columns'), td:has-text('Manage Columns')")
            if col_picker:
                await col_picker.click()
                await page.wait_for_timeout(600)
                source_chk = await page.query_selector("input[type='checkbox'][value*='Source'], label:has-text('Source') input")
                if not source_chk:
                    labels = await page.query_selector_all("label, span, td")
                    for lbl in labels:
                        txt = (await lbl.inner_text() or "").strip()
                        if txt.lower() == "source":
                            await lbl.scroll_into_view_if_needed()
                            source_chk = await lbl.query_selector("input[type='checkbox']")
                            if not source_chk:
                                parent = await lbl.evaluate_handle("el => el.closest('tr, li, div')")
                                if parent:
                                    source_chk = await parent.query_selector("input[type='checkbox']")
                            break
                if source_chk:
                    await source_chk.scroll_into_view_if_needed()
                    if not await source_chk.is_checked():
                        await source_chk.check()
                        logger.info("[Robie Exporter] 'Source' column successfully checked.")
                # Close dropdown
                await page.keyboard.press("Escape")

            # 5. Click View Report
            logger.info("[Robie Exporter] Generating report dataset...")
            view_btn = await page.query_selector("input[value='View Report'], button:has-text('View Report')")
            if view_btn:
                await view_btn.click()
                await page.wait_for_timeout(5000)

            # 6. Trigger CSV Export
            logger.info("[Robie Exporter] Triggering CSV export...")
            async with page.expect_download(timeout=60000) as download_info:
                export_menu = await page.query_selector("a[title='Export'], div[id*='Export'], button:has-text('Export')")
                if export_menu:
                    await export_menu.click()
                    await page.wait_for_timeout(500)
                    csv_opt = await page.query_selector("a:has-text('CSV'), a:has-text('CSV (comma delimited)')")
                    if csv_opt:
                        await csv_opt.click()
                else:
                    # Alternative selector
                    await page.click("text='CSV (comma delimited)'")

            download: Download = await download_info.value
            await download.save_as(str(dest_file))
            logger.info(f"[Robie Exporter] Successfully saved daily report to {dest_file}")

            await browser.close()
            return dest_file
