import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://natgenagency.com/MainMenu.aspx', wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(3)
            print('Current URL:', page.url)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_session_check.png')
        finally:
            await page.close()

asyncio.run(run())
