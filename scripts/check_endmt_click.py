import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'PolicyEndorsements' in page.url or 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            handler = await nat_page.evaluate('''() => {
                const el = document.getElementById('ctl00_MainContent_btnContinueNewOrPendingEndmt');
                if (window.$ && $._data) {
                    const ev = $._data(el, 'events');
                    if (ev && ev.click) {
                        return ev.click.map(h => h.handler.toString()).join(' --- ');
                    }
                }
                return 'no click handler';
            }''')
            print('Click handler:', handler)

asyncio.run(main())
