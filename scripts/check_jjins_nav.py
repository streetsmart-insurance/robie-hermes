import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://www.jjins.com/', wait_until='domcontentloaded')
            await asyncio.sleep(2)
            
            accept_btn = page.locator("button:has-text('Accept All')").first
            if await accept_btn.count() > 0:
                await accept_btn.click()
                print('Clicked Accept All')
                await asyncio.sleep(1)
                
            login_link = page.locator("a:has-text('Login')").first
            print('Login link href/text:', await login_link.get_attribute('href'), await login_link.inner_text())
            await login_link.hover()
            await asyncio.sleep(1)
            
            # Print visible dropdown items
            dropdown_items = await page.locator("ul.sub-menu a, .dropdown-menu a, li:has-text('Login') a").all()
            for item in dropdown_items:
                print('Menu item:', await item.get_attribute('href'), await item.inner_text())
                
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_login_hover.png')
        finally:
            await page.close()

asyncio.run(run())
