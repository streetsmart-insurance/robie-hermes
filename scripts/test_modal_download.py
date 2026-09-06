import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def test_modal_dl():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await btn.click()
        await asyncio.sleep(1)
        
        dl_btn = await page.query_selector("button:has-text('Download video')")
        await dl_btn.click()
        await asyncio.sleep(2)
        
        # Now find the download button inside the modal dialog
        modal_dl = await page.query_selector("[role='dialog'] button:has-text('Download video'), div[class*='modal'] button:has-text('Download')")
        if modal_dl:
            print("Found modal Download button! Clicking with expect_download...")
            try:
                async with page.expect_download(timeout=15000) as dl_info:
                    await modal_dl.click()
                dl = await dl_info.value
                out_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/{dl.suggested_filename}"
                await dl.save_as(out_path)
                print("SUCCESSFULLY DOWNLOADED BOP VIDEO:", out_path)
            except Exception as e:
                print("Modal download event error:", e)
                # Check what modal says
                txt = await page.evaluate("() => document.querySelector(\"[role='dialog']\")?.innerText")
                print("Modal text after click:", txt)
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(test_modal_dl())
