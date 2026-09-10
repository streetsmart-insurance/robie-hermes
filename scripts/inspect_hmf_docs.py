import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        # Check Underwriting Notes / Docs
        print('Clicking Underwriting Notes / Docs...')
        uw_tab = main_frame.locator('text="Underwriting Notes / Docs"').first
        await uw_tab.click()
        await asyncio.sleep(3)
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_uw_docs.png')
        uw_text = await main_frame.evaluate('() => document.body.innerText')
        print('UW Notes/Docs:\n', uw_text[:1000])
        
        # Check Application Notes / Docs
        print('Clicking Application Notes / Docs...')
        app_tab = main_frame.locator('text="Application Notes / Docs"').first
        await app_tab.click()
        await asyncio.sleep(3)
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_app_docs.png')
        app_text = await main_frame.evaluate('() => document.body.innerText')
        print('App Notes/Docs:\n', app_text[:1000])

        # Check Policy History
        print('Clicking Policy History...')
        hist_tab = main_frame.locator('text="Policy History"').first
        await hist_tab.click()
        await asyncio.sleep(3)
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_policy_history.png')
        hist_text = await main_frame.evaluate('() => document.body.innerText')
        print('Policy History:\n', hist_text[:1000])

asyncio.run(run())
