#!/usr/bin/env python3
"""RingCentral Headless Playwright Reports Crawler.

Navigates to RingCentral Admin Portal / Analytics, filters call logs,
and exports the Call Log CSV directly into /tmp/ringcentral_reports
for automated, zero-cost productivity auditing.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("robie.ringcentral_reports_crawler")

RC_REPORTS_DIR = Path(os.environ.get("ROBIE_RINGCENTRAL_REPORTS_DIR", "/tmp/ringcentral_reports"))


class RingCentralReportsCrawler:
    """Automates Call Log exports in RingCentral portal via Playwright."""

    def __init__(
        self,
        output_dir: Path = RC_REPORTS_DIR,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_call_log_csv(self, page: Any, date_range_days: int = 1) -> Optional[Path]:
        """Navigates to RingCentral Call Log page and exports the CSV."""
        try:
            logger.info("Navigating to RingCentral Call Log portal...")
            # RingCentral Admin Call Log URL
            page.goto("https://service.ringcentral.com/application/company/callLog", wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            # Check if login or sign-in is required
            if "login" in page.url or "signin" in page.url:
                logger.warning("RingCentral session requires authentication. Current URL: %s", page.url)

            # Look for Export / Download button
            export_btn = page.locator("button:has-text('Export'), [data-test-automation-id*='export'], button[aria-label*='Export'], a:has-text('Export')")
            if export_btn.count() > 0:
                with page.expect_download(timeout=30000) as download_info:
                    export_btn.first.click()
                download = download_info.value
                dest = self.output_dir / f"RingCentral_CallLog_{int(time.time())}.csv"
                download.save_as(str(dest))
                logger.info("Exported RingCentral Call Log to %s", dest)
                return dest
            else:
                logger.warning("Export button not found on RingCentral Call Log page.")
                return None
        except Exception as e:
            logger.error("Failed to export RingCentral Call Log: %s", e)
            return None
