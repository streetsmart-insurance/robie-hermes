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
            
            user_input = page.locator('input:visible, input[type=text]:visible').first
            print('Filling user_input:', await user_input.get_attribute('id'), await user_input.get_attribute('name'))
            await user_input.fill('Carlof')
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_step1_filled.png')
            
            sign_in_btn = page.locator('button:visible, a:visible, input[type=submit]:visible').filter(has_text='SIGN IN').first
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
                if any(w in line.lower() for w in ['password', 'code', 'verify', 'security', 'welcome', 'error', 'sign']):
                    print('  ', line.strip())
                    
            # Check for password input
            pw_input = page.locator('input[type=password]:visible')
            if await pw_input.count() > 0:
                print('Found password field! Filling password...')
                await pw_input.first.fill('moxry8-dihzyw-Bognec')
                await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_pw_filled.png')
                next_btn = page.locator('button:visible, input[type=submit]:visible, a:visible').filter(has_text='SIGN IN').first
                if await next_btn.count() > 0:
                    print('Submitting password...')
                    await next_btn.click()
                    await asyncio.sleep(6)
                    await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_logged_in.png')
                    print('Final NatGen URL:', page.url)
        finally:
            await page.close()

asyncio.run(run())
