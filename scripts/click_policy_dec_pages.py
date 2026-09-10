import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        
        docframe = None
        for f in aspire_page.frames:
            if f.name == 'docframe':
                docframe = f
                break
                
        if not docframe:
            print('No docframe found')
            return
            
        print('Clicking POLICY DEC PAGES in docframe...')
        dec_tab = docframe.locator('text="POLICY DEC PAGES"').first
        await dec_tab.click()
        await asyncio.sleep(6)
        
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_policy_dec_pages.png')
        
        # Check all child frames or embeds/objects/iframes
        for f in aspire_page.frames:
            print(f'Frame: {f.name}, URL: {f.url}')
            
        embeds = await docframe.locator('embed, iframe, object, a').all()
        for e in embeds:
            src = await e.get_attribute('src') or await e.get_attribute('href') or ''
            tag = await e.evaluate('el => el.tagName')
            print(f'Element {tag}: src/href={src}')

asyncio.run(run())
