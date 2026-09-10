import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        # Find all elements containing 'your user has been added' or 'Jake Ferrara'
        target = page.locator('span:has-text("Fwd: MerchantsGroup.com: your user")').first
        if await target.count() > 0:
            print("Found target, clicking with force=True...")
            await target.click(force=True)
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_merchants_open.png")
            
            # Extract all message bodies
            bodies = await page.evaluate('''() => {
                const els = Array.from(document.querySelectorAll('.ii.gt, .a3s.aiL'));
                return els.map(e => e.innerText);
            }''')
            print("=== MESSAGE BODIES ===")
            for i, b in enumerate(bodies):
                print(f"--- MSG {i} ---")
                print(b)
        else:
            print("Target not found.")

if __name__ == "__main__":
    asyncio.run(check())
