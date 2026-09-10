import asyncio
from playwright.async_api import async_playwright

async def run():
    print('Testing National General portal access on hermes-poc-01...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to NatGen portal...')
            await page.goto('https://www.natgenagency.com/Login.aspx', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_landing.png')
            print('Saved data/screenshots/natgen_landing.png')
            print('NatGen URL:', page.url)
            
            # Check inputs
            user_input = page.locator('input[name*=User i], input[id*=User i]').first
            pass_input = page.locator('input[type=password]').first
            
            print(f'User input count: {await user_input.count()}, Pass count: {await pass_input.count()}')
            if await user_input.count() > 0 and await pass_input.count() > 0:
                print('Found login form!')
                await user_input.fill('Carlof')
                await pass_input.fill('moxry8-dihzyw-Bognec')
                await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_filled.png')
                submit_btn = page.locator('button[type=submit], input[type=submit], button:has-text(Log In), input[value*=Log In i]').first
                if await submit_btn.count() > 0:
                    print('Clicking login button...')
                    await submit_btn.click()
                    await asyncio.sleep(6)
                    await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_after_login.png')
                    print('Saved data/screenshots/natgen_after_login.png')
                    print('Post-login NatGen URL:', page.url)
        finally:
            await page.close()

asyncio.run(run())
