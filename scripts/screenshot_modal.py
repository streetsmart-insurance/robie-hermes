import asyncio
from playwright.async_api import async_playwright

sid = "50e461126ea5412fa87289ab06cd3469"

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        more_btn = await page.wait_for_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await more_btn.click()
        await asyncio.sleep(2)
        
        dl_item = await page.wait_for_selector("button:has-text('Download video'), [role='menuitem']:has-text('Download')")
        print("Clicking dl_item...")
        await dl_item.click()
        await asyncio.sleep(2)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/comm_pkg_modal.png")
        
        # Check all visible text
        txt = await page.evaluate("() => document.body.innerText")
        lines = [l.strip() for l in txt.split('\\n') if l.strip()]
        for l in lines:
            if any(k in l.lower() for k in ["download", "quality", "720p"]):
                print("Visible text match:", l)
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
