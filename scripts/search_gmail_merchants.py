import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check_mail():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#search/Cardone", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_cardone_search.png")
        
        # Extract email subjects and snippets
        emails = await page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('tr[role="row"], .zA'));
            return rows.map(r => ({
                sender: r.querySelector('.yX')?.innerText || '',
                subject: r.querySelector('.y6')?.innerText || '',
                snippet: r.querySelector('.y2')?.innerText || '',
                date: r.querySelector('.xW')?.innerText || ''
            }));
        }''')
        print(f"Found {len(emails)} emails matching 'Cardone':")
        for e in emails[:10]:
            print(e)
            
        # Click the first matching email from Merchants if present
        first_row = page.locator('tr[role="row"]:has-text("Merchants"), tr[role="row"]:has-text("midlantic"), tr[role="row"]:has-text("Cardone")').first
        if await first_row.is_visible():
            await first_row.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_cardone_open_email.png")
            body_text = await page.evaluate('''() => {
                const b = document.querySelector('.a3s.aiL, div[role="listitem"]');
                return b ? b.innerText : document.body.innerText;
            }''')
            print("--- EMAIL CONTENT ---")
            print(body_text[:2000])

        await page.close()

if __name__ == "__main__":
    asyncio.run(check_mail())
