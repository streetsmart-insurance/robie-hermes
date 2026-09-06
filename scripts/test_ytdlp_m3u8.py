import asyncio
from playwright.async_api import async_playwright
import subprocess

sid = "50e461126ea5412fa87289ab06cd3469"

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        master_url = None
        def on_req(req):
            nonlocal master_url
            if "playlist-multibitrate.m3u8" in req.url or "masterplaylist.m3u8" in req.url:
                master_url = req.url
        page.on("request", on_req)
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        await page.evaluate('''() => {
            const v = document.querySelector("video");
            if (v) { v.muted = true; v.play(); }
        }''')
        await asyncio.sleep(3)
        await page.close()
        
        print("Captured URL:", master_url)
        if master_url:
            cmd = [
                "/Users/carloferrara/Library/Python/3.9/bin/yt-dlp",
                master_url,
                "-o", "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos/commercial_package.mp4",
                "--no-check-certificates"
            ]
            print("Running yt-dlp...")
            res = subprocess.run(cmd, capture_output=True, text=True)
            print("yt-dlp stdout:", res.stdout)
            print("yt-dlp stderr:", res.stderr)

if __name__ == "__main__":
    asyncio.run(check())
