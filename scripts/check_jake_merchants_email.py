import asyncio
import logging
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
                
        # Search for Jake's forwarded email
        await page.goto("https://mail.google.com/mail/u/0/#search/from%3AJake+merchants", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        
        # Click the first search result
        clicked = await page.evaluate('''() => {
            const rows = document.querySelectorAll('tr[role="row"], .zA');
            if (rows.length > 0) {
                rows[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                return true;
            }
            return false;
        }''')
        print("Clicked Jake email:", clicked)
        await asyncio.sleep(3)
        
        body_text = await page.evaluate('''() => {
            const b = document.querySelector('.a3s.aiL') || document.querySelector('div[role="main"]');
            return b ? b.innerText : document.body.innerText;
        }''')
        print("--- JAKE FORWARDED BODY ---")
        print(body_text)
        
        # Also let's search for "password" or "welcome" or "user ID" or "activation" from Merchants
        await page.goto("https://mail.google.com/mail/u/0/#search/merchants+password+OR+welcome+OR+username+OR+profile", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_pw_search.png")
        
        rows = await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('tr[role="row"], .zA'));
            return items.map(el => ({
                text: el.innerText.replace(/\\s+/g, ' ').trim(),
                sender: el.querySelector('.yX')?.innerText || '',
                subject: el.querySelector('.y6')?.innerText || '',
                snippet: el.querySelector('.y2')?.innerText || '',
                date: el.querySelector('.xW')?.innerText || ''
            }));
        }''')
        print(f"\n--- PW/WELCOME SEARCH ROWS ({len(rows)}) ---")
        for i, r in enumerate(rows[:10]):
            print(f"[{i}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    asyncio.run(check())
