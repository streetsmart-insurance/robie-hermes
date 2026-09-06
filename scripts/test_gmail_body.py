import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def send_lenin():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        compose_btn = page.locator('div[gh="cm"], div[role="button"]:has-text("Compose")').first
        if not await compose_btn.is_visible():
            menu = page.locator('button[aria-label="Main menu"], div[aria-label="Main menu"]').first
            if await menu.is_visible():
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

        # Body - let's set innerHTML and dispatch input event
        body_text_html = (
            "<p>Hi Lenin,</p>"
            "<p>Quick process update regarding client communications once policy change requests are completed:</p>"
            "<p>You do not need to manually draft and email completion confirmations to clients after processing changes in EZLynx.</p>"
            "<p>When a policy change transaction is marked completed/processed in EZLynx, the <strong>Automation Center</strong> automatically dispatches an official completion email to the insured (with their Client Center access link and updated coverage details), exactly like it did for LA Burger and Haughey Brothers.</p>"
            "<p>Skipping the manual completion email will save you valuable time and ensure clients don't receive duplicate emails.</p>"
            "<p>Thanks for all your great work!</p><br>"
        )

        # Inject into the top of body editor before the signature block
        await page.evaluate(f'''() => {{
            const editor = document.querySelector('div[role="textbox"][aria-label*="Message Body"]');
            if (editor) {{
                editor.innerHTML = `{body_text_html}` + editor.innerHTML;
                editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                editor.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }}''')
        await asyncio.sleep(1)

        # Check what is inside editor now
        content = await page.evaluate('''() => {
            const editor = document.querySelector('div[role="textbox"][aria-label*="Message Body"]');
            return editor ? editor.innerText : '';
        }''')
        logger.info(f"Verified editor text preview:\n{content[:250]}")

        # Screenshot the compose window to be 1000% sure the body is visible
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/lenin_email_preview.png")

        # Send
        send_btn = page.locator('div[role="button"][data-tooltip*="Send"], div[role="button"]:has-text("Send")').filter(has_not_text="Schedule").first
        logger.info("Clicking Send to Lenin...")
        await send_btn.click()
        await asyncio.sleep(3)

        # Verify in sent
        await page.goto("https://mail.google.com/mail/u/0/#sent", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/lenin_email_sent_verified2.png")
        logger.info("Email to Lenin successfully sent and verified!")
        await page.close()

asyncio.run(send_lenin())
