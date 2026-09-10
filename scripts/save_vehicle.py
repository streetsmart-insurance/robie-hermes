import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            print('Selecting Ownership Status: Owned...')
            await nat_page.locator('#ctl00_MainContent_ucInsuredAuto_ddlOwnershipStatus').select_option(label='Owned')
            await asyncio.sleep(2)
            
            # Check if any new fields appeared
            inputs = await nat_page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input, select, textarea'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        id: el.id,
                        name: el.name,
                        value: el.value,
                        type: el.type
                    }));
            }""")
            print(f'Total visible inputs: {len(inputs)}')
            
            # Click Save
            print('Clicking Save vehicle button...')
            await nat_page.locator('#ctl00_MainContent_ucInsuredAuto_btnSave').click()
            await asyncio.sleep(6)
            
            print('Landed URL:', nat_page.url)
            print('Page Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[25:120]:
                print('  ', l)
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_vehicle_saved.png')

asyncio.run(main())
