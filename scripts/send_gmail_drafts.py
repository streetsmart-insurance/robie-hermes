import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def send_drafts():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#drafts", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        targets = [
            "Policy Change Request Follow-Up: Shoreline Builders",
            "Policy Change Request Follow-Up: Cardone Electric"
        ]

        for target in targets:
            logger.info(f"Looking for draft matching '{target}'...")
            await page.goto("https://mail.google.com/mail/u/0/#drafts", wait_until="domcontentloaded")
            await asyncio.sleep(2.5)

            # Find draft row
            row = page.locator("tr[role='row']").filter(has_text=target).first
            if not await row.is_visible():
                logger.warning(f"Draft '{target}' not found in Drafts list!")
                continue

            await row.click()
            await asyncio.sleep(2)

            # Click Send button
            # In Gmail compose/draft, Send button is div[role="button"][data-tooltip*="Send"] or div:has-text("Send")
            send_btn = page.locator('div[role="button"][data-tooltip*="Send"], div[role="button"]:has-text("Send")').filter(has_not_text="Schedule").first
            if await send_btn.is_visible():
                logger.info(f"Clicking Send for '{target}'...")
                await send_btn.click()
                await asyncio.sleep(3)
                logger.info(f"Sent '{target}' successfully!")
            else:
                logger.error(f"Send button not visible for '{target}'")

        # Capture sent screenshot
        await page.goto("https://mail.google.com/mail/u/0/#sent", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_sent_verified.png")
        logger.info("Captured sent folder verification screenshot.")
        await page.close()

asyncio.run(send_drafts())
