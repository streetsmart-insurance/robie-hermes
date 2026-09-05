"""Attach to open Chrome via CDP, monitor IVANS Exchange login, and extract Connections/LOB matrix."""

import os
import sys
import time
import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ivans_monitor")

OUTPUT_DIR = Path("data/ivans")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

async def monitor_ivans():
    logger.info("Connecting to Chrome over CDP on http://localhost:9222...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        
        # Find IVANS page
        target_page = None
        for page in context.pages:
            url = page.url
            if "ivans.com" in url:
                target_page = page
                break
        
        if not target_page:
            logger.info("No IVANS page open yet. Opening https://exchange.ivans.com...")
            target_page = await context.new_page()
            await target_page.goto("https://exchange.ivans.com")

        logger.info(f"Attached to tab: {target_page.url}")
        print("\n" + "="*70)
        print(">>> CHROME WINDOW IS OPEN ON YOUR SCREEN AT THE IVANS LOGIN PAGE <<<")
        print("Please enter your IVANS username and password in the Chrome window.")
        print("Complete any 2FA / MFA verification.")
        print("Waiting for login redirect to exchange.ivans.com...")
        print("="*70 + "\n")

        # Wait for redirect away from login.ivans.com to exchange.ivans.com
        logged_in = False
        for i in range(120):  # Wait up to 10 minutes (120 x 5s)
            current_url = target_page.url
            logger.info(f"[{i*5}s] Current URL: {current_url}")
            
            if "exchange.ivans.com" in current_url and "login" not in current_url:
                try:
                    content = await target_page.content()
                    if any(term in content for term in ["Sign Out", "Log Out", "Connections", "Dashboard", "Feedback", "Carrier", "Reports"]):
                        logged_in = True
                        break
                except Exception:
                    pass
            
            await asyncio.sleep(5)

        if not logged_in:
            logger.warning("Timed out waiting for login to complete.")
            return False

        logger.info("LOGIN DETECTED! Authenticated to IVANS Exchange!")
        await target_page.wait_for_timeout(3000)

        # Capture verification screenshot
        screenshot_path = OUTPUT_DIR / "ivans_logged_in.png"
        await target_page.screenshot(path=str(screenshot_path))
        logger.info(f"Saved confirmation screenshot to {screenshot_path}")

        # Look for Connections / Reports / Download Services
        logger.info("Inspecting navigation links for Connections / Reports...")
        links = await target_page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a, button, [role="tab"]')).map(el => ({
                text: el.innerText.trim(),
                href: el.getAttribute('href'),
                tag: el.tagName
            })).filter(x => x.text.length > 0);
        }""")
        for l in links[:30]:
            logger.info(f"Nav item: {l['text']} -> {l.get('href')}")

        return True

if __name__ == "__main__":
    asyncio.run(monitor_ivans())
