import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def test_dl():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await btn.click()
        await asyncio.sleep(1)
        
        # Check properties of Download video button
        dl_info = await page.evaluate('''() => {
            const btn = Array.from(document.querySelectorAll("button, [role='menuitem']")).find(el => el.innerText.includes("Download video"));
            if (!btn) return null;
            return {
                text: btn.innerText,
                disabled: btn.disabled,
                ariaDisabled: btn.getAttribute('aria-disabled'),
                tag: btn.tagName
            };
        }''')
        print("Download video button info:", dl_info)
        
        # Try clicking it with download listener
        try:
            async with page.expect_download(timeout=10000) as dl_event:
                dl_btn = await page.query_selector("text='Download video'")
                await dl_btn.click()
            dl = await dl_event.value
            out_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/{dl.suggested_filename}"
            await dl.save_as(out_path)
            print("Successfully downloaded video:", out_path)
        except Exception as e:
            print("Download event error/modal:", e)
            # Maybe it opens a resolution modal? Let's check page HTML / screenshot
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/dl_modal.png")
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(test_dl())
