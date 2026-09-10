import asyncio
import os
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
            
            # Click expand chevron or policy title
            title_link = page.locator("a:has-text('TNF4012536')").first
            if await title_link.count() > 0:
                print('Found title link, clicking...')
                await title_link.click()
                await asyncio.sleep(4)
            else:
                card = page.locator("text='TNF4012536'").first
                await card.click()
                await asyncio.sleep(4)
                
            os.makedirs('/opt/renewal-automation-system/data/screenshots', exist_ok=True)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_policy_details.png')
            print('Saved manganello_policy_details.png')
            print('Current URL:', page.url)
            
            body = await page.evaluate('() => document.body.innerText')
            for line in body.splitlines():
                if any(k in line.lower() for k in ['term', 'effective', '10/05/2026', '10/5/2026', '2027', 'renew', 'pending', 'status']):
                    print('  Detail:', line.strip())
        finally:
            await page.close()

if __name__ == '__main__':
    asyncio.run(run())
