import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = [x for x in ctx.pages if "chat.google.com" in x.url][0]
        
        # Navigate to search query in chat
        await page.goto("https://chat.google.com/search/merchants", wait_until="networkidle")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/chat_search_merchants.png")
        text = await page.evaluate("() => document.body.innerText")
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for l in lines[:30]:
            print(l)

if __name__ == "__main__":
    asyncio.run(run())
