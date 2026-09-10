import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://app.maple-tech.com/hmf/login_link.aspx', wait_until='domcontentloaded')
            await asyncio.sleep(2)
            content = await page.content()
            print('Page HTML snippet:')
            print(content[:2000])
        finally:
            await page.close()

asyncio.run(run())
