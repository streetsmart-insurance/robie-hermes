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
            await asyncio.sleep(4)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_policies_final_proof.png')
            print('Saved manganello_policies_final_proof.png')
            
            # Print card details
            body = await page.evaluate('() => document.body.innerText')
            for line in body.splitlines():
                if any(k in line.lower() for k in ['tnf', 'flood', '1,982', '1982', '10/5/2026', '10/5/2027', 'active', 'pending']):
                    print('  Policy summary line:', line.strip())
        finally:
            await page.close()

asyncio.run(run())
