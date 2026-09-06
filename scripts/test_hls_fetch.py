import asyncio
from playwright.async_api import async_playwright

sid = "50e461126ea5412fa87289ab06cd3469"

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        m3u8_url = None
        def on_req(req):
            nonlocal m3u8_url
            if ".m3u8" in req.url:
                m3u8_url = req.url
        page.on("request", on_req)
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(5)
        
        # Click play to trigger streaming
        play_btn = await page.query_selector("button[aria-label*='Play'], div[aria-label*='Play'], button:has-text('Play')")
        if play_btn:
            await play_btn.click()
            await asyncio.sleep(2)
            
        print("Captured m3u8 URL:", m3u8_url)
        
        if m3u8_url:
            content = await page.evaluate(f"""async () => {{
                const r = await fetch("{m3u8_url}");
                return await r.text();
            }}""")
            print("=== M3U8 Content ===")
            print(content[:500])
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
