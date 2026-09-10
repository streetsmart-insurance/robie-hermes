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
                
        # Click the row with text "9:13 PM"
        row = page.locator('tr:has-text("9:13 PM")').first
        if not await row.count():
            row = page.locator('div[role="row"]:has-text("9:13 PM")').first
            
        print("Found row for 9:13 PM, clicking...")
        await row.click()
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_913_email.png")
        
        # Read the message pane
        content = await page.evaluate('''() => {
            const msgs = Array.from(document.querySelectorAll('.ii.gt, .a3s.aiL, div[role="main"]'));
            return msgs.map(m => m.innerText);
        }''')
        
        print("=== CONTENT OF 9:13 PM EMAIL ===")
        for idx, c in enumerate(content):
            print(f"--- MSG {idx} ---")
            print(c)
            
        # Extract all links
        links = await page.evaluate('''() => {
            const body = document.querySelector('div[role="main"]') || document.body;
            return Array.from(body.querySelectorAll('a')).map(a => ({ text: a.innerText.trim(), href: a.href }));
        }''')
        print("=== LINKS ===")
        for l in links:
            if 'merchants' in l['href'].lower() or 'token' in l['href'].lower() or 'password' in l['href'].lower():
                print(l)

if __name__ == "__main__":
    asyncio.run(check())
