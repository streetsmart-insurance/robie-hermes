import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        
        # Check if there is already a gmail page
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        if not page:
            page = await ctx.new_page()
            await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
            await asyncio.sleep(3)
        else:
            await page.bring_to_front()
            
        print("Current page URL:", page.url)
        
        # Search query for merchants
        # Navigate directly to search URL
        await page.goto("https://mail.google.com/mail/u/0/#search/merchants", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_merchants_search_page.png")
        
        # Extract rows
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
        
        print(f"Total search result rows: {len(rows)}")
        for i, r in enumerate(rows):
            print(f"[{i}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")
            
        # If there's an email with login, invitation, credentials, or merchants:
        # Let's inspect the first 3
        for i in range(min(5, len(rows))):
            row_loc = page.locator('tr[role="row"], .zA').nth(i)
            print(f"\n--- OPENING EMAIL {i} ---")
            await row_loc.click()
            await asyncio.sleep(3)
            await page.screenshot(path=f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_merchants_email_{i}.png")
            
            body = await page.evaluate('''() => {
                const b = document.querySelector('.a3s.aiL, div[role="listitem"]');
                return b ? b.innerText : document.body.innerText;
            }''')
            print(body[:2000])
            
            # Go back to search
            await page.go_back()
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(check())
