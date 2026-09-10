import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        # Find the page that is currently at Policy/Actions/Renew
        pages = ctx.pages
        renew_page = None
        for pg in pages:
            if 'Policy/Actions/Renew' in pg.url:
                renew_page = pg
                break
                
        if not renew_page:
            renew_page = await ctx.new_page()
            await renew_page.goto('https://app.ezlynx.com/applicantportal/Policy/Actions/Renew/143979332/56639575', wait_until='domcontentloaded')
            await asyncio.sleep(3)

        print('Renew page URL:', renew_page.url)
        
        # Click #RenewPolicyBtn
        btn = renew_page.locator('#RenewPolicyBtn')
        print('Clicking #RenewPolicyBtn...')
        
        # Monitor dialogs
        renew_page.on('dialog', lambda d: print('Dialog appeared:', d.message, d.type))
        
        await btn.click()
        print('Clicked! Waiting 8 seconds for save/redirect...')
        await asyncio.sleep(8)
        
        print('URL after click:', renew_page.url)
        await renew_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_after_click_renew.png')
        print('Saved manganello_after_click_renew.png')

asyncio.run(run())
