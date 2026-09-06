import asyncio
from playwright.async_api import async_playwright
import json
import os

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def trigger_all():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        for i, item in enumerate(catalog):
            lob = item.get("lob")
            sid = item.get("session_id")
            
            # Skip the first two which are already downloaded
            if sid in ["0f1a1709f5264e15939b0c048cea6028", "dc0d74328429498183e342992fcb4aa7"]:
                print(f"[{i+1}/{len(catalog)}] {lob} already downloaded. Skipping.")
                continue
                
            print(f"\n[{i+1}/{len(catalog)}] Triggering {lob} ({sid})...")
            try:
                await page.goto(f"https://www.loom.com/share/{sid}")
                await asyncio.sleep(4)
                
                # 1. Transcript generate if needed
                t_tab = await page.query_selector("[role='tab']:has-text('Transcript'), button:has-text('Transcript')")
                if t_tab:
                    await t_tab.click()
                    await asyncio.sleep(1)
                    gen_btn = await page.query_selector("button:has-text('Generate')")
                    if gen_btn:
                        await gen_btn.click()
                        print("  -> Triggered Transcript generation!")
                        
                # 2. More actions -> Download video
                more_btn = await page.wait_for_selector("[data-testid='toggleActions'], button:has-text('More actions')", timeout=10000)
                await more_btn.click()
                await asyncio.sleep(1.5)
                
                dl_item = await page.wait_for_selector("button:has-text('Download video')", timeout=10000)
                await dl_item.click()
                await asyncio.sleep(2)
                
                # 3. Click modal Download video button
                clicked = await page.evaluate('''() => {
                    const btns = Array.from(document.querySelectorAll("button"));
                    const modalBtns = btns.filter(b => b.innerText.trim() === "Download video" || b.innerText.trim() === "Downloading...");
                    if (modalBtns.length > 0) {
                        const target = modalBtns[modalBtns.length - 1];
                        target.click();
                        return { success: true, text: target.innerText };
                    }
                    return { success: false };
                }''')
                print(f"  -> Modal trigger result: {clicked}")
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"  -> Error on {lob}: {e}")
                
        await page.close()
        print("\nAll remaining videos triggered for transcoding!")

if __name__ == "__main__":
    asyncio.run(trigger_all())
