import asyncio
from playwright.async_api import async_playwright

async def run():
    print('Testing J&J portal access on hermes-poc-01...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to J&J portal...')
            await page.goto('https://www.jjins.com/auth0_home/', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_landing.png')
            print('Saved data/screenshots/jjins_landing.png')
            print('J&J URL:', page.url)
            
            # Check inputs
            user_input = page.locator('input[type=email], input[name*=user i], input[id*=user i], input[name*=email i]').first
            pass_input = page.locator('input[type=password]').first
            
            if await user_input.count() > 0 and await pass_input.count() > 0:
                print('Found login form!')
                await user_input.fill('jake@ssinj.com')
                await pass_input.fill('Zap1410')
                submit_btn = page.locator('button[type=submit], input[type=submit], button:has-text(Log In)').first
                if await submit_btn.count() > 0:
                    print('Clicking log in...')
                    await submit_btn.click()
                    await asyncio.sleep(6)
                    await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_after_login.png')
                    print('Saved data/screenshots/jjins_after_login.png')
                    print('Post-login URL:', page.url)
        finally:
            await page.close()

asyncio.run(run())
