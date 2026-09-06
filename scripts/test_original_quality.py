import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def test_orig():
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
        
        # Check select/dropdown inside modal
        options = await page.evaluate('''() => {
            const selects = Array.from(document.querySelectorAll("[role='dialog'] select, [role='dialog'] [role='combobox'], [role='dialog'] button"));
            return selects.map(s => ({ tag: s.tagName, text: s.innerText, aria: s.getAttribute('aria-label') }));
        }''')
        print("Modal elements:", options)
        
        await page.close()

if __name__ == "__main__":
    asyncio.run(test_orig())
