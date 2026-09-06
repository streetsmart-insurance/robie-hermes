import asyncio
from playwright.async_api import async_playwright

async def read_htmlview():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        url = "https://docs.google.com/spreadsheets/d/1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo/htmlview"
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        text = await page.inner_text("body")
        print("Sheet Body Text:")
        print(text)
        await page.close()

asyncio.run(read_htmlview())
