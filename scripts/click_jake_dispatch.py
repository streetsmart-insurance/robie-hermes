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
                
        clicked = await page.evaluate('''() => {
            const rows = document.querySelectorAll('tr[role="row"], .zA');
            for (const r of rows) {
                if (r.innerText.includes("Jake Ferrara") && r.innerText.includes("your user has been added")) {
                    r.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    return true;
                }
            }
            return false;
        }''')
        print("Clicked Jake row:", clicked)
        await asyncio.sleep(4)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_email_opened.png")
        
        # Extract body
        bodies = await page.evaluate('''() => {
            const els = Array.from(document.querySelectorAll('.ii.gt, .a3s.aiL, div[role="main"]'));
            return els.map(e => e.innerText);
        }''')
        print("=== JAKE EMAIL BODIES ===")
        for i, b in enumerate(bodies):
            print(f"--- MSG {i} ---")
            print(b)
            
        links = await page.evaluate('''() => {
            const pane = document.querySelector('div[role="main"]') || document.body;
            return Array.from(pane.querySelectorAll('a')).map(a => ({ text: a.innerText.trim(), href: a.href }));
        }''')
        print("=== LINKS ===")
        for l in links:
            if 'merchants' in l['href'].lower() or 'token' in l['href'].lower():
                print(l)

if __name__ == "__main__":
    asyncio.run(check())
