import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        print('Clicking HONJ2025100027-26...')
        policy_link = main_frame.locator('text="HONJ2025100027-26"').first
        await policy_link.click()
        await asyncio.sleep(6)
        
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_policy_details.png')
        
        # Check all frames
        for frame in aspire_page.frames:
            print(f'Frame: {frame.name}, URL: {frame.url}')
            
        text = await main_frame.evaluate('() => document.body.innerText')
        print('fraTocMain text snippet:\n', text[:1500])

asyncio.run(run())
