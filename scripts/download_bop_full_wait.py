import asyncio
from playwright.async_api import async_playwright
import os

sid = "dc0d74328429498183e342992fcb4aa7"

async def do_download():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        # Enable CDP downloads to /Users/carloferrara/Documents/antigravity/happy-fermi/data
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allowAndName",
            "downloadPath": "/Users/carloferrara/Documents/antigravity/happy-fermi/data",
            "eventsEnabled": True
        })
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(5)
        
        # Click More actions
        more_btn = await page.wait_for_selector("[data-testid='toggleActions'], button:has-text('More actions')", timeout=10000)
        await more_btn.click()
        await asyncio.sleep(1)
        
        dl_item = await page.wait_for_selector("text='Download video'", timeout=10000)
        await dl_item.click()
        await asyncio.sleep(2)
        
        modal_dl = await page.wait_for_selector("[role='dialog'] button:has-text('Download video')", timeout=10000)
        print("Found modal download button, clicking...")
        await modal_dl.click()
        
        print("Waiting for download to finish (up to 120s)...")
        for i in range(40):
            await asyncio.sleep(3)
            # Check if any new mp4 file appeared in data/
            files = [f for f in os.listdir("/Users/carloferrara/Documents/antigravity/happy-fermi/data") if f.endswith(".mp4") and "bop" in f.lower() or "busines" in f.lower()]
            # Also check downloads or crdownload
            all_files = os.listdir("/Users/carloferrara/Documents/antigravity/happy-fermi/data")
            pending = [f for f in all_files if ".crdownload" in f or ".tmp" in f]
            txt = await page.evaluate("() => document.querySelector(\"[role='dialog']\")?.innerText")
            if (i % 5 == 0):
                print(f"[{i*3}s] Modal: {repr(txt)} | Pending: {pending} | Files: {files}")
            if files:
                print("FOUND DOWNLOADED FILE:", files)
                break
                
        await asyncio.sleep(5)
        await page.close()

if __name__ == "__main__":
    asyncio.run(do_download())
