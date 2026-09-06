import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def check():
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
        
        q_btn = await page.query_selector("button:has-text('720p (default)')")
        if q_btn:
            print("Clicking 720p dropdown...")
            await q_btn.click()
            await asyncio.sleep(1)
            items = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll("[role='option'], [role='menuitem'], li")).map(e => e.innerText.trim()).filter(Boolean);
            }''')
            print("Dropdown options:", items)
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
