import asyncio
from playwright.async_api import async_playwright

sid = "dc0d74328429498183e342992fcb4aa7"

async def gen():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        # Click transcript tab
        t_tab = await page.query_selector("[role='tab']:has-text('Transcript'), button:has-text('Transcript')")
        if t_tab:
            await t_tab.click()
            await asyncio.sleep(2)
            
        gen_btn = await page.query_selector("button:has-text('Generate')")
        if gen_btn:
            print("Found Generate button! Clicking...")
            await gen_btn.click()
            await asyncio.sleep(5)
            
            # Check what happens
            status_text = await page.evaluate('''() => {
                const el = document.querySelector("[role='tabpanel'], [class*='transcript']");
                return el ? el.innerText : '';
            }''')
            print("Status after clicking generate:", status_text)
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(gen())
