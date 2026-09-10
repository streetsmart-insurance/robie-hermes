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
            print('Clicking Add Vehicle button...')
            add_btn = nat_page.locator('#ctl00_MainContent_ucInsuredAutoSchedule_btnAddVehicle')
            await add_btn.click()
            await asyncio.sleep(6)
            print('URL:', nat_page.url)
            print('Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[30:160]:
                print('  ', l)
            # Find inputs
            inputs = await nat_page.evaluate('''() => {
                return Array.from(document.querySelectorAll('input, select, textarea, button, a'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        name: el.name,
                        type: el.type,
                        text: el.innerText ? el.innerText.trim() : el.value,
                        value: el.value
                    })).filter(x => x.id || x.name || x.text);
            }''')
            print('--- Form Inputs ---')
            for i in inputs:
                if any(w in (str(i['id']) + str(i['name']) + str(i['text'])).lower() for w in ['vin', 'year', 'make', 'model', 'use', 'garage', 'save', 'continue', 'decode', 'verify', 'submit', 'liability']):
                    print(' ', i)
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_add_vehicle_form.png')

asyncio.run(main())
