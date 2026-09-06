import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def sniff():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        requests = []
        def on_req(req):
            if "loom.com" in req.url or "cdn" in req.url or "video" in req.url:
                requests.append(f"{req.method} {req.url}")
        page.on("request", on_req)
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(6)
        
        # Click play button if present
        play_btn = await page.query_selector("[aria-label*='Play'], button:has-text('Play')")
        if play_btn:
            print("Clicking play...")
            await play_btn.click()
            await asyncio.sleep(3)
            
        print(f"Total requests intercepted: {len(requests)}")
        for r in requests:
            if any(k in r for k in ["api", "video", "transcoded", "mp4", "m3u8", "graphql"]):
                print("  ->", r[:120])
                
        await page.close()

asyncio.run(sniff())
