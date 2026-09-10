import asyncio
from playwright.async_api import async_playwright

async def run():
    print('Testing NatGen Step 1 & 2...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://natgenagency.com/Login.aspx', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(3)
            
            user_input = page.locator('input').first
            await user_input.fill('Carlof')
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_step1_filled.png')
            
            sign_in_btn = page.locator('button:has-text(SIGN IN), a:has-text(SIGN IN), input[value*=SIGN IN]').first
            print('Clicking SIGN IN...')
            await sign_in_btn.click()
            await asyncio.sleep(5)
            
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_step2.png')
            print('Saved data/screenshots/natgen_step2.png')
            print('URL:', page.url)
            
            # Check for password field or OTP
            body = await page.evaluate('() => document.body.innerText')
            print('Page text summary:')
            for line in body.splitlines():
                if any(w in line.lower() for w in ['password', 'code', 'verify', 'security', 'welcome', 'error']):
                    print('  ', line.strip())
        finally:
            await page.close()

asyncio.run(run())
