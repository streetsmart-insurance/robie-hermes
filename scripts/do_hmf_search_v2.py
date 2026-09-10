import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        print('Filling pno with HONJ2025100027...')
        pno_input = main_frame.locator('input#pno')
        await pno_input.fill('HONJ2025100027')
        
        print('Submitting via span#submit...')
        submit_span = main_frame.locator('span#submit')
        await submit_span.click()
        await asyncio.sleep(6)
        
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_search_results_v2.png')
        text = await main_frame.evaluate('() => document.body.innerText')
        print('Result text snippet:\n', text[:1500])

asyncio.run(run())
