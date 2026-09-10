import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check_mail():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#search/Cardone", wait_until="networkidle")
        await asyncio.sleep(5)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_cardone_search2.png")
        
        # Check text on page
        text = await page.evaluate("() => document.body.innerText")
        for line in text.split("\n"):
            if any(k in line.lower() for k in ["cardone", "merchants", "capi075976", "rutledge", "alexander"]):
                print("Line:", line)
                
        # Click on any email that has Merchants or Cardone
        mail_row = page.locator('tr[role="row"]:has-text("Cardone"), tr[role="row"]:has-text("Merchants")').first
        if await mail_row.is_visible():
            print("Clicking email row...")
            await mail_row.click()
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_cardone_email_view.png")
            body = await page.evaluate("() => document.querySelector('.a3s.aiL')?.innerText || document.body.innerText")
            print("--- EMAIL BODY ---")
            print(body[:3000])

        await page.close()

if __name__ == "__main__":
    asyncio.run(check_mail())
