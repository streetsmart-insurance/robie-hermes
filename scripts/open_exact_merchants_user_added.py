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
                
        # Search specifically for subject
        await page.goto("https://mail.google.com/mail/u/0/#search/subject%3A%22your+user+has+been+added+successfully%22", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_user_added_search.png")
        
        # Count results
        rows = await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('tr[role="row"], .zA'));
            return items.map((el, idx) => ({
                idx: idx,
                sender: el.querySelector('.yX')?.innerText || '',
                subject: el.querySelector('.y6')?.innerText || '',
                date: el.querySelector('.xW')?.innerText || ''
            }));
        }''')
        print("Found rows:", rows)
        
        # Click row 0
        if len(rows) > 0:
            print("Clicking row 0...")
            await page.evaluate('''() => {
                const rows = document.querySelectorAll('tr[role="row"], .zA');
                if (rows.length > 0) rows[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            }''')
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_email_row0.png")
            
            body = await page.evaluate('''() => {
                const b = document.querySelector('.a3s.aiL') || document.querySelector('div[role="main"]');
                return b ? b.innerText : document.body.innerText;
            }''')
            print("--- ROW 0 BODY ---")
            print(body)
            
            # Now let's see if there are expanded messages in this thread
            msgs = await page.evaluate('''() => {
                const elements = Array.from(document.querySelectorAll('.ii.gt'));
                return elements.map(el => el.innerText);
            }''')
            for i, m in enumerate(msgs):
                print(f"--- THREAD MSG {i} ---")
                print(m)

if __name__ == "__main__":
    asyncio.run(check())
