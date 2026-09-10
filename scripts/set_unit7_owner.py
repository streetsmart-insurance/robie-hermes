import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'PolicyAutoOwners' in page.url or 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            print('Setting Unit 7 Registered Owner to JESUS SOLANO...')
            await nat_page.locator('#ctl00_MainContent_Owners_rpOwners_ctl06_ddlDrivers').select_option(label='JESUS SOLANO')
            await asyncio.sleep(2)
            
            # Find all clickable buttons or links at the bottom
            btns = await nat_page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input[type=submit], input[type=button], button, a'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        id: el.id,
                        name: el.name,
                        type: el.type,
                        text: el.innerText ? el.innerText.trim() : el.value,
                        href: el.getAttribute('href')
                    })).filter(x => x.text);
            }""")
            print('Clickable actions:')
            for b in btns:
                if any(w in b['text'].lower() for w in ['save', 'continue', 'next', 'previous', 'cover', 'rate']):
                    print(' ', b)
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_unit7_owner_set.png')

asyncio.run(main())
