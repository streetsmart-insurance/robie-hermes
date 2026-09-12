import asyncio
from playwright.async_api import async_playwright

async def snap():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.pages[0]
        print("URL:", page.url)
        print("Title:", await page.title())
        await page.screenshot(path="/tmp/login_screen.png")

asyncio.run(snap())