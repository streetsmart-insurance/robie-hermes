import asyncio
from playwright.async_api import async_playwright
import json

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        
        # Look for EZLynx tab
        ez_page = None
        for page in ctx.pages:
            if "ezlynx.com" in page.url:
                ez_page = page
                break
                
        if not ez_page:
            print("No active EZLynx tab found. Creating new page...")
            ez_page = await ctx.new_page()
            await ez_page.goto("https://app.ezlynx.com")
            await asyncio.sleep(5)
            
        print("EZLynx Page URL:", ez_page.url)
        print("EZLynx Page Title:", await ez_page.title())

if __name__ == "__main__":
    asyncio.run(check())
