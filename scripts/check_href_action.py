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
            action = await nat_page.evaluate('() => window.hrefAction || "not defined"')
            print('hrefAction:', action)
            dialog_btns = await nat_page.locator('.ui-dialog button, button:has-text("Yes")').all_inner_texts()
            print('Dialog buttons:', dialog_btns)

asyncio.run(main())
