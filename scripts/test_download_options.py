import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def test_downloads():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        # Open More actions
        btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await btn.click()
        await asyncio.sleep(1)
        
        # Set up download listener
        async with page.expect_download(timeout=10000) as download_info:
            caption_btn = await page.query_selector("text='Download captions'")
            if caption_btn:
                print("Clicking 'Download captions'...")
                await caption_btn.click()
            else:
                print("Download captions button not found!")
                
        download = await download_info.value
        path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/{download.suggested_filename}"
        await download.save_as(path)
        print(f"Captions downloaded to: {path}")
        with open(path) as f:
            print("Captions preview:\n", f.read()[:500])
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(test_downloads())
