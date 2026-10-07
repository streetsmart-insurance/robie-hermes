#!/usr/bin/env python3
"""Magellan AI Headless Playwright Reports Crawler.

Navigates to Magellan AI (https://app.magellan.insure), filters sentiment
and call transcripts, and exports the CSV / JSON report directly into
/tmp/magellan_reports for automated sentiment auditing and churn prevention.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("robie.magellan_crawler")

MAGELLAN_REPORTS_DIR = Path(os.environ.get("ROBIE_MAGELLAN_REPORTS_DIR", "/tmp/magellan_reports"))


class MagellanReportsCrawler:
    """Automates Call Sentiment & Intent Tag exports from Magellan AI."""

    def __init__(
        self,
        output_dir: Path = MAGELLAN_REPORTS_DIR,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_sentiment_report(self, page: Any) -> Optional[Path]:
        """Navigates to Magellan AI dashboard/calls and exports the sentiment analysis."""
        try:
            logger.info("Navigating to Magellan AI portal...")
            page.goto("https://app.magellan.insure/calls", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            # Check if login or SSO is required
            if "login" in page.url or "signin" in page.url or "auth" in page.url:
                logger.warning("Magellan AI session requires authentication. Current URL: %s", page.url)

            # Look for Export / Download button
            export_btn = page.locator("button:has-text('Export'), [data-testid*='export'], button[aria-label*='Export'], a:has-text('Export')")
            if export_btn.count() > 0:
                with page.expect_download(timeout=30000) as download_info:
                    export_btn.first.click()
                download = download_info.value
                dest = self.output_dir / f"Magellan_Sentiment_{int(time.time())}.csv"
                download.save_as(str(dest))
                logger.info("Exported Magellan AI sentiment report to %s", dest)
                return dest
            else:
                logger.warning("Export button not found on Magellan AI page.")
                return None
        except Exception as e:
            logger.error("Failed to export Magellan AI report: %s", e)
            return None
