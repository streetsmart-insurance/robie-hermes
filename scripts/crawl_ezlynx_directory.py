"""Crawler for EZLynx Carrier Directory entries via Chrome Remote Debugging (port 9222).

Extracts:
- Carrier Name & ID
- Document Download phrasing/setting
- Download or Manual phrasing/setting
- Underwriter contacts and email addresses
- Endorsement & Loss Run emails
- Portal URLs / notes
"""

import asyncio
import json
import logging
import os
import re
from typing import Dict, Any, List
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ezlynx_directory_crawler")

OUTPUT_PATH = "data/ezlynx_directory_mapping.json"

async def crawl_directory():
    async with async_playwright() as p:
        logger.info("Connecting to Chrome on http://localhost:9222...")
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            logger.error(f"Failed to connect to Chrome on port 9222: {e}")
            return

        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "ezlynx.com" in pg.url:
                page = pg
                break

        if not page:
            page = await ctx.new_page()
            await page.goto("https://app.ezlynx.com/web/directory")

        # Check if login is required
        if "auth/account/login" in page.url:
            logger.warning("EZLynx is currently at the Login page. Please complete login in the open Chrome window.")
            print("\n>>> PLEASE LOG IN TO EZLYNX IN THE OPEN CHROME WINDOW <<<\n")
            # Wait up to 120 seconds for login to succeed
            for _ in range(60):
                await asyncio.sleep(2)
                if "auth/account/login" not in page.url and "ezlynx.com" in page.url:
                    logger.info("Login detected! Proceeding to Directory...")
                    break
            else:
                logger.error("Timed out waiting for login.")
                return

        logger.info(f"Navigating to Directory: {page.url}")
        if "/web/directory" not in page.url:
            await page.goto("https://app.ezlynx.com/web/directory")
            await page.wait_for_timeout(3000)

        logger.info(f"Current page URL: {page.url}, Title: {await page.title()}")

        # Look for table or search input in directory
        await page.wait_for_timeout(2000)
        content = await page.content()
        with open("data/ezlynx_directory_page.html", "w") as f:
            f.write(content)
        logger.info("Saved directory page HTML to data/ezlynx_directory_page.html")

        # Extract carrier rows / links
        entries = await page.evaluate('''() => {
            const links = Array.from(document.querySelectorAll('a[href*="/web/directory/entry/"]'));
            return links.map(a => ({
                text: a.innerText.trim(),
                href: a.href,
                id: a.href.split('/').pop()
            }));
        }''')
        logger.info(f"Found {len(entries)} directory entry links on current view.")
        return entries

if __name__ == "__main__":
    asyncio.run(crawl_directory())
