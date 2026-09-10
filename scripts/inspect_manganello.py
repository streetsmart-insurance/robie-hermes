import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to Manganello policies page...')
            await page.goto('https://app.ezlynx.com/web/account/143979332/policies', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_policies_current.png')
            print('Saved manganello_policies_current.png')
            
            body = await page.evaluate('() => document.body.innerText')
            for line in body.splitlines():
                if any(k in line.lower() for k in ['tnf', 'flood', '1982', 'active', 'pending']):
                    print('  Policy line:', line.strip())
        finally:
            await page.close()

asyncio.run(run())
