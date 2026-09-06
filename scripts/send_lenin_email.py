import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def send_to_lenin():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        logger.info("Opening compose window...")
        compose_btn = page.locator('div[gh="cm"], div[role="button"]:has-text("Compose")').first
        if not await compose_btn.is_visible():
            menu = page.locator('button[aria-label="Main menu"], div[aria-label="Main menu"]').first
            if await menu.is_visible():
                await menu.click()
                await asyncio.sleep(1)
        
        await compose_btn.click()
        await asyncio.sleep(2)

        # Recipient
        to_input = page.locator('input[aria-label="To recipients"], input[peoplekit-id]').first
        if not await to_input.is_visible():
            to_input = page.locator('input[role="combobox"]').first
        await to_input.fill("lenin@streetsmart.insurance")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        # Subject
        subj_input = page.locator('input[name="subjectbox"]')
        await subj_input.fill("Policy Change Workflow: Client Completion Notifications Handled by Automation Center")
        await asyncio.sleep(0.5)

        # Body
        body_text = (
            "Hi Lenin,\n\n"
            "Quick process update regarding client communications once policy change requests are completed:\n\n"
            "You do not need to manually draft and email completion confirmations to clients after processing changes in EZLynx.\n\n"
            "When a policy change transaction is processed/completed in EZLynx, Automation Center automatically dispatches an official completion email to the insured (including access to their Client Center and updated policy details), just as it did for LA Burger and Haughey Brothers.\n\n"
            "Dropping the manual completion email will save you time and prevent the client from receiving duplicate notifications.\n\n"
            "Thanks for all your hard work on these changes!\n\n"
        )

        body_box = page.locator('div[role="textbox"][aria-label*="Message Body"]').first
        await body_box.click()
        await page.keyboard.type(body_text)
        await asyncio.sleep(1)

        # Send
        send_btn = page.locator('div[role="button"][data-tooltip*="Send"], div[role="button"]:has-text("Send")').filter(has_not_text="Schedule").first
        logger.info("Clicking Send to Lenin...")
        await send_btn.click()
        await asyncio.sleep(3)

        # Verify in sent
        await page.goto("https://mail.google.com/mail/u/0/#sent", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/lenin_email_sent_verified.png")
        logger.info("Email to Lenin sent and verified!")
        await page.close()

asyncio.run(send_to_lenin())
