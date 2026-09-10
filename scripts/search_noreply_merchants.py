import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        await page.goto("https://mail.google.com/mail/u/0/#search/from%3ANoReply-Merchants", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/noreply_merchants_search.png")
        
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
        print(f"Results for from:NoReply-Merchants: {len(rows)}")
        for r in rows[:10]:
            print(f"[{r['idx']}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    asyncio.run(run())
