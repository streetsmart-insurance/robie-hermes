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
        if btn:
            print("Clicking More actions...")
            await btn.click()
            await asyncio.sleep(2)
            
            menu_items = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll("[role='menuitem'], [role='option'], button, a")).map(e => ({
                    text: e.innerText ? e.innerText.trim() : '',
                    testid: e.getAttribute('data-testid') || '',
                    aria: e.getAttribute('aria-label') || ''
                })).filter(e => e.text || e.testid || e.aria);
            }''')
            for m in menu_items:
                s = f"{m['text']} | {m['testid']} | {m['aria']}"
                if any(k in s.lower() for k in ["download", "duplicate", "export", "delete", "archive", "move"]):
                    print("Menu action:", s)
                    
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
