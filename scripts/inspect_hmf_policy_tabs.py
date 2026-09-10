import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        # Click Premiums tab
        print('Clicking Premiums tab...')
        prem_tab = main_frame.locator('text="Premiums"').first
        await prem_tab.click()
        await asyncio.sleep(3)
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_premiums_tab.png')
        prem_text = await main_frame.evaluate('() => document.body.innerText')
        print('Premiums text snippet:\n', prem_text[:1500])

asyncio.run(run())
