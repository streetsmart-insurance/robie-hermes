import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://app.ezlynx.com/web/account/143979332/policies', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(3)
            
            # Click Table view button
            tbl_btn = page.locator('button:has-text(Table view)')
            if await tbl_btn.count() > 0:
                await tbl_btn.click()
                await asyncio.sleep(2)
                
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_policies_table.png')
            print('Saved manganello_policies_table.png')
            
            body = await page.evaluate('() => document.body.innerText')
            for line in body.splitlines():
                if any(k in line.lower() for k in ['tnf', 'flood', '1982', '2026', '2027']):
                    print('  Table line:', line.strip())
        finally:
            await page.close()

asyncio.run(run())
