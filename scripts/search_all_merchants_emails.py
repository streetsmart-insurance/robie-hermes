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
                
        # Search in:anywhere from:merchantsgroup.com
        await page.goto("https://mail.google.com/mail/u/0/#search/in%3Aanywhere+from%3Amerchantsgroup.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_all_search.png")
        
        # Get list of emails
        rows = await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('tr[role="row"], .zA'));
            return items.map((el, idx) => ({
                idx: idx,
                sender: el.querySelector('.yX')?.innerText || '',
                subject: el.querySelector('.y6')?.innerText || '',
                snippet: el.querySelector('.y2')?.innerText || '',
                date: el.querySelector('.xW')?.innerText || ''
            }));
        }''')
        print(f"Total found from merchantsgroup.com: {len(rows)}")
        for r in rows[:15]:
            print(f"[{r['idx']}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    asyncio.run(check())
