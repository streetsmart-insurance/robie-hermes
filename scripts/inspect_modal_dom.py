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
        
        more_btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        print("More btn:", more_btn)
        if more_btn:
            await more_btn.click()
            await asyncio.sleep(1.5)
            
            dl_item = await page.query_selector("button:has-text('Download video')")
            print("DL item in menu:", dl_item)
            if dl_item:
                await dl_item.click()
                await asyncio.sleep(2)
                
                # Print all buttons on page
                buttons = await page.evaluate('''() => {
                    return Array.from(document.querySelectorAll("button")).map(b => ({
                        text: b.innerText.trim(),
                        aria: b.getAttribute('aria-label'),
                        testid: b.getAttribute('data-testid'),
                        parentRole: b.parentElement ? b.parentElement.getAttribute('role') : null
                    })).filter(b => b.text || b.aria);
                }''')
                print("All buttons after clicking Download video:")
                for b in buttons:
                    if any(k in f"{b['text']} {b['aria']}".lower() for k in ["download", "quality", "720p", "cancel", "close"]):
                        print("  ->", b)
                        
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
