#!/usr/bin/env python3
"""EZLynx Headless Playwright Reports Crawler.

Navigates to EZLynx Reporting Center, generates, and exports:
1. Task Aging Report (Open, Completed, Overdue tasks by CSR/Account Manager)
2. User Activity Summary Report (Notes, Quotes, Policy Changes by CSR)

Saves CSV exports directly into /tmp/ezlynx_reports for automated ingestion.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("robie.ezlynx_reports_crawler")

REPORTS_DIR = Path(os.environ.get("ROBIE_EZLYNX_REPORTS_DIR", "/tmp/ezlynx_reports"))


class EZLynxReportsCrawler:
    """Automates report exports in EZLynx using Playwright."""

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9222",
        output_dir: Path = REPORTS_DIR,
    ):
        self.cdp_url = cdp_url
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_task_aging_report(self, page: Any) -> Optional[Path]:
        """Navigates to EZLynx Tasks / Task Aging report and downloads CSV."""
        try:
            logger.info("Navigating to EZLynx Tasks report...")
            # EZLynx tasks management / reporting URL
            page.goto("https://app.ezlynx.com/web/tasks/management", wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)

            # Look for Export / Download CSV action
            export_btn = page.locator("button:has-text('Export'), a:has-text('Export'), [aria-label*='Export'], #btnExport")
            if export_btn.count() > 0:
                with page.expect_download(timeout=30000) as download_info:
                    export_btn.first.click()
                download = download_info.value
                dest = self.output_dir / f"EZLynx_Task_Aging_Report_{int(time.time())}.csv"
                download.save_as(str(dest))
                logger.info("Exported Task Aging Report to %s", dest)
                return dest
            else:
                logger.warning("Export button not found on Task Management page.")
                return None
        except Exception as e:
            logger.error("Failed to export Task Aging Report: %s", e)
            return None

    def export_activity_summary_report(self, page: Any) -> Optional[Path]:
        """Navigates to EZLynx User Activity Summary report and downloads CSV."""
        try:
            logger.info("Navigating to EZLynx Activity Log report...")
            page.goto("https://app.ezlynx.com/web/reports/activity-log", wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)

            export_btn = page.locator("button:has-text('Export'), a:has-text('Export'), [aria-label*='Export'], #btnExport")
            if export_btn.count() > 0:
                with page.expect_download(timeout=30000) as download_info:
                    export_btn.first.click()
                download = download_info.value
                dest = self.output_dir / f"EZLynx_Activity_Summary_{int(time.time())}.csv"
                download.save_as(str(dest))
                logger.info("Exported Activity Summary Report to %s", dest)
                return dest
            else:
                logger.warning("Export button not found on Activity Log page.")
                return None
        except Exception as e:
            logger.error("Failed to export Activity Summary Report: %s", e)
            return None

    def run_daily_export_suite(self, page: Any) -> Tuple[Optional[Path], Optional[Path]]:
        """Runs full suite of daily reports and stores in intake directory."""
        task_path = self.export_task_aging_report(page)
        act_path = self.export_activity_summary_report(page)
        return task_path, act_path
