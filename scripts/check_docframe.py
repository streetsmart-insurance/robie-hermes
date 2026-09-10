import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        doc_src = await main_frame.evaluate("() => document.getElementById('docframe') ? document.getElementById('docframe').src : null")
        print('docframe src:', doc_src)
        
        for f in aspire_page.frames:
            if f.name == 'docframe':
                print('docframe URL:', f.url)
                body = await f.evaluate('() => document.body.innerHTML')
                print('docframe innerHTML snippet:\n', body[:500])

asyncio.run(run())
