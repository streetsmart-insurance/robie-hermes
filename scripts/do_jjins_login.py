import asyncio
from playwright.async_api import async_playwright

async def run():
    print('Executing Johnson & Johnson login...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://agent.mga-portal.com/home', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            
            print('Filling email...')
            await page.locator('input[name="email"]').fill('jake@ssinj.com')
            await asyncio.sleep(1)
            
            print('Filling password...')
            await page.locator('input[name="password"]').fill('Zap1410')
            await asyncio.sleep(1)
            
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_filled.png')
            
            print('Clicking LOG IN...')
            login_btn = page.locator('button:has-text("LOG IN")').first
            await login_btn.click()
            await asyncio.sleep(8)
            
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_after_login.png')
            print('URL after login:', page.url)
            print('Title after login:', await page.title())
            body = await page.evaluate('() => document.body.innerText')
            print('Body text snippet:\n', body[:1000])
            
        finally:
            await page.close()

if __name__ == '__main__':
    asyncio.run(run())
