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
        
        elements = await page.evaluate('''() => {
            const els = Array.from(document.querySelectorAll("button, a, [role='button']"));
            return els.map(e => ({
                text: e.innerText ? e.innerText.trim() : '',
                aria: e.getAttribute('aria-label') || '',
                testid: e.getAttribute('data-testid') || '',
                href: e.getAttribute('href') || ''
            }));
        }''')
        
        for el in elements:
            s = f"{el.get('text')} | {el.get('aria')} | {el.get('testid')} | {el.get('href')}"
            if any(k in s.lower() for k in ["download", "more", "dots", "menu", "export", "share"]):
                print("Match:", s)
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
