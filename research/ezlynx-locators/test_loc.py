import asyncio
from playwright.async_api import async_playwright

async def test_loc():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        
        await page.goto("https://app.ezlynx.com/web/account/220250093/documents", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        
        sel = 'tr:has(#document-checkbox-813253571-input) button:has-text("Add label")'
        loc = page.locator(sel)
        cnt = await loc.count()
        print(f"Selector: {sel} -> count={cnt}")
        
        sel2 = 'tr:has(#document-checkbox-813253571-input)'
        cnt2 = await page.locator(sel2).count()
        print(f"Row Selector: {sel2} -> count={cnt2}")

asyncio.run(test_loc())
