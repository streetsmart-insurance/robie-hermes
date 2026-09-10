import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = [x for x in ctx.pages if "mail.google.com" in x.url][0]
        
        # Search for "after:2026/09/06 merchants"
        url = "https://mail.google.com/mail/u/0/?q=merchants#search/after%3A2026%2F09%2F06"
        print("Navigating...")
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        rows = await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('tr[role="row"], .zA'));
            return items.map((el, idx) => ({
                idx: idx,
                sender: el.querySelector('.yX')?.innerText.replace(/\\s+/g, ' ').trim() || '',
                subject: el.querySelector('.y6')?.innerText.replace(/\\s+/g, ' ').trim() || '',
                snippet: el.querySelector('.y2')?.innerText.replace(/\\s+/g, ' ').trim() || '',
                date: el.querySelector('.xW')?.innerText.replace(/\\s+/g, ' ').trim() || ''
            }));
        }''')
        print(f"Total rows: {len(rows)}")
        for r in rows:
            print(f"[{r['idx']}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    asyncio.run(run())
