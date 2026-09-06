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

        compose_btn = page.locator('div[gh="cm"], div[role="button"]:has-text("Compose")').first
        if not await compose_btn.is_visible():
            menu = page.locator('button[aria-label="Main menu"]').first
            await menu.click()
            await asyncio.sleep(1)

        await compose_btn.click()
        await asyncio.sleep(2)

        # To
        to_input = page.locator('input[aria-label="To recipients"], input[peoplekit-id], input[role="combobox"]').first
        await to_input.fill("lenin@streetsmart.insurance")
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        # Subject
        subj_input = page.locator('input[name="subjectbox"]')
        await subj_input.fill("Policy Change Workflow Update: Client Completion Emails Handled by Automation Center")
        await asyncio.sleep(0.5)

        # Body
        body_box = page.locator('div[role="textbox"][aria-label*="Message Body"]').first
        await body_box.focus()
        await page.keyboard.press("Home")
        
        email_body = (
            "Hi Lenin,\n\n"
            "Quick process update regarding client communications once policy change requests are completed:\n\n"
            "You do not need to manually draft and email completion confirmations to clients after processing changes in EZLynx.\n\n"
            "When a policy change transaction is marked completed/processed in EZLynx, the Automation Center automatically dispatches an official completion email to the insured (with their Client Center access link and updated coverage details), exactly like it did for LA Burger and Haughey Brothers.\n\n"
            "Skipping the manual completion email will save you valuable time and ensure clients don't receive duplicate emails.\n\n"
            "Thanks for all your great work!\n\n"
        )
        await page.keyboard.insert_text(email_body)
        await asyncio.sleep(1)

        # Verify text is actually in the box
        verified_text = await body_box.inner_text()
        logger.info(f"Verified body content in Gmail compose box:\n{verified_text[:200]}")

        # Capture preview screenshot before sending
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/lenin_email_preview_verified.png")

        # Click Send
        send_btn = page.locator('div[role="button"][data-tooltip*="Send"], div[role="button"]:has-text("Send")').filter(has_not_text="Schedule").first
        logger.info("Clicking Send to Lenin...")
        await send_btn.click()
        await asyncio.sleep(3)

        # Go to sent to verify
        await page.goto("https://mail.google.com/mail/u/0/#sent", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/lenin_email_sent_verified.png")
        logger.info("Email to Lenin successfully sent and verified!")
        await page.close()

asyncio.run(send_to_lenin())
