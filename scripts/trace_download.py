import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def trace():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        # Configure CDP session to allow downloads
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": "/Users/carloferrara/Documents/antigravity/happy-fermi/data/downloads"
        })
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await btn.click()
        await asyncio.sleep(1)
        
        dl_btn = await page.query_selector("button:has-text('Download video')")
        await dl_btn.click()
        await asyncio.sleep(2)
        
        modal_dl = await page.query_selector("[role='dialog'] button:has-text('Download video'), div[class*='modal'] button:has-text('Download')")
        print("Found modal_dl button:", await modal_dl.inner_text())
        
        await modal_dl.click()
        print("Clicked modal_dl button! Monitoring for 30s...")
        for i in range(15):
            await asyncio.sleep(2)
            txt = await page.evaluate("() => document.querySelector(\"[role='dialog']\")?.innerText")
            print(f"Sec {(i+1)*2}: {txt}")
            if not txt:
                print("Modal closed!")
                break
                
        await asyncio.sleep(5)
        await page.close()

if __name__ == "__main__":
    asyncio.run(trace())
