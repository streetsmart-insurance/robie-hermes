import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def debug_dl():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        reqs = []
        page.on("request", lambda r: reqs.append(f"{r.method} {r.url}"))
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await btn.click()
        await asyncio.sleep(1)
        
        dl_btn = await page.query_selector("button:has-text('Download video')")
        if dl_btn:
            print("Clicking Download video...")
            await dl_btn.click()
            await asyncio.sleep(3)
            
            # Check for any new elements, dialogs, or popups
            dialog = await page.evaluate('''() => {
                const modal = document.querySelector("[role='dialog'], [class*='modal'], [class*='download']");
                return modal ? modal.innerText : null;
            }''')
            print("Modal/Dialog text:", dialog)
            
            # Check requests made after click
            print("Requests after click:")
            for r in reqs[-15:]:
                print("  ", r[:100])
                
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/after_dl_click.png")
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(debug_dl())
