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
                
        # Target the second row in the search list
        rows = page.locator('tr[role="row"], .zA')
        count = await rows.count()
        print("Total rows:", count)
        for i in range(count):
            text = await rows.nth(i).inner_text()
            if "Jake Ferrara" in text or "6:43" in text:
                print(f"Row {i} matches Jake! Clicking...")
                await rows.nth(i).click()
                await asyncio.sleep(3)
                await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_thread_opened.png")
                
                # Get message contents
                body = await page.evaluate('''() => {
                    const els = Array.from(document.querySelectorAll('.ii.gt, .a3s.aiL'));
                    return els.map(e => e.innerText);
                }''')
                print("=== JAKE EMAIL BODY ===")
                for b_item in body:
                    print(b_item)
                break

if __name__ == "__main__":
    asyncio.run(check())
