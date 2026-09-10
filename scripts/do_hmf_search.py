import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        print('Filling pno with HONJ2025100027...')
        await main_frame.locator('input#pno').fill('HONJ2025100027')
        
        print('Clicking input[value="SEARCH"]...')
        search_btn = main_frame.locator('input[value="SEARCH"]').first
        await search_btn.click()
        await asyncio.sleep(5)
        
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_search_results_success.png')
        text = await main_frame.evaluate('() => document.body.innerText')
        print('Result snippet:\n', text[:1000])

asyncio.run(run())
