import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'PolicyEndmtSummary' in page.url or 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            print('Clicking Submit on PolicyEndmtSummary...')
            submit_btn = nat_page.locator('#ctl00_MainContent_btnSubmit')
            await submit_btn.click()
            await asyncio.sleep(10)
            
            print('Landed URL:', nat_page.url)
            print('Page Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[:120]:
                print('  ', l)
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_endorsement_confirmation.png')

asyncio.run(main())
