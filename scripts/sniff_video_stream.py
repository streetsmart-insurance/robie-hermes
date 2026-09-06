import asyncio
from playwright.async_api import async_playwright

sid = "50e461126ea5412fa87289ab06cd3469"

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        all_m3u8 = []
        def on_req(req):
            if ".m3u8" in req.url or ".mp4" in req.url:
                all_m3u8.append(req.url)
        page.on("request", on_req)
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        # Click play to start video streaming
        await page.evaluate('''() => {
            const v = document.querySelector("video");
            if (v) { v.muted = true; v.play(); }
        }''')
        await asyncio.sleep(4)
        
        print("Captured video/audio streaming URLs:")
        for u in all_m3u8:
            print("  ->", u.split("?")[0])
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
