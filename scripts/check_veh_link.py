import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            veh_link = nat_page.locator('a:has-text(\"Vehicles\")')
            print('Vehicles link count:', await veh_link.count())
            for i in range(await veh_link.count()):
                el = veh_link.nth(i)
                print('  [' + str(i) + '] href:', await el.get_attribute('href'), 'text:', await el.inner_text())

asyncio.run(main())
