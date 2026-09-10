import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'PolicyEndorsements' in page.url or 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            print('Clicking Yes button in dialog...')
            yes_btn = nat_page.locator('.dialog-buttons-container button,button:has-text("Yes")')
            if await yes_btn.count() > 0:
                await yes_btn.first.click()
            else:
                await nat_page.evaluate('() => window.__doPostBack("ctl00$MainContent$btnContinueNewOrPendingEndmt", "")')
            await asyncio.sleep(8)
            print('URL after Yes:', nat_page.url)
            print('Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[:200]:
                print('  ', l)
            await nat_page.screenshot(path='/opt/revewal-automation-system/data/screenshots/natgen_endmt_step2_page.png')

asyncio.run(main())
