import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://natgenagency.com/Login.aspx', wait_until='domcontentloaded')
            await asyncio.sleep(2)
            await page.locator('input:visible, input[type=text]:visible').first.fill('Carlof')
            await page.locator('button:visible, a:visible, input[type=submit]:visible').filter(has_text='SIGN IN').first.click()
            await asyncio.sleep(4)
            await page.locator('input[type=password]:visible').first.fill('moxry8-dihzyw-Bognec')
            await page.locator('button:visible, input[type=submit]:visible, a:visible').filter(has_text='SIGN IN').first.click()
            await asyncio.sleep(5)
            
            print('Current URL:', page.url)
            options = await page.locator('a, button, input[type=radio], div[role=button], li, tr').all()
            for opt in options:
                is_vis = await opt.is_visible()
                txt = (await opt.inner_text()).strip() if is_vis else ''
                if any(k in txt.lower() for k in ['streetsmart', 'email', 'text', 'code']):
                    tag = await opt.evaluate('el => el.tagName')
                    html = await opt.evaluate('el => el.outerHTML.substring(0, 200)')
                    print(f'Option [{tag}]: {txt[:60]} -> {html}')
        finally:
            await page.close()

asyncio.run(run())
