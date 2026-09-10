import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check_mail():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        # Type into search input
        search_box = page.locator('input[aria-label="Search mail"], input[placeholder="Search mail"]').first
        if await search_box.is_visible():
            await search_box.click()
            await search_box.fill("Cardone")
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_cardone_results.png")
            
            # Print visible text
            body_text = await page.evaluate("() => document.body.innerText")
            print("--- SEARCH RESULTS ---")
            for line in body_text.split("\n"):
                if any(k in line.lower() for k in ["cardone", "merchants", "alexander", "rutledge", "perdomo", "capi075976"]):
                    print("  Line:", line.strip())
                    
            # Also search for 'merchants'
            await search_box.click()
            await search_box.fill("merchantsgroup")
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_merchants_results.png")
            body_text2 = await page.evaluate("() => document.body.innerText")
            print("--- MERCHANTS SEARCH RESULTS ---")
            for line in body_text2.split("\n"):
                if any(k in line.lower() for k in ["cardone", "merchants", "alexander", "rutledge", "perdomo", "capi075976"]):
                    print("  Merch Line:", line.strip())

        await page.close()

if __name__ == "__main__":
    asyncio.run(check_mail())
