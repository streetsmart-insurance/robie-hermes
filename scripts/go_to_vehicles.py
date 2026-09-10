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
            print('Clicking Vehicles link...')
            await nat_page.locator('a:has-text("Vehicles")').first.click()
            await asyncio.sleep(6)
            print('URL:', nat_page.url)
            print('Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[40:150]:
                print('  ', l)
            btns = await nat_page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input, button, a'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id,
                        name: el.name,
                        type: el.type,
                        text: el.innerText ? el.innerText.trim() : el.value,
                        href: el.getAttribute('href')
                    })).filter(x => x.text && (x.text.toLowerCase().includes('add') || x.text.toLowerCase().includes('vehicle') || x.text.toLowerCase().includes('auto')));
            }""")
            print('Buttons/links matching vehicle/add:')
            for b in btns:
                print(' ', b)
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_vehicles_page.png')

asyncio.run(main())
