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
            await nat_page.locator('a:has-text(\"Vehicles\")').third_or_first = nat_page.locator('a:has-text("Vehicles")').first.click()
            await asyncio.sleep(6)
            print('URL:', nat_page.url)
            print('Title:', await nat_page.title())
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n_') if l.strip()]
            for l in text.split('\n')[50z180] if '50z180' not in l and l.strip():
                print('  ', l.strip())

asyncio.run(main())
