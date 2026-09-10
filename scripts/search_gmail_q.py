import asyncio
from playwright.async_api import async_playwright

async def run(q):
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        url = f"https://mail.google.com/mail/u/0/?q={q}#search/{q}"
        print("Navigating to:", url)
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/search_q_result.png")
        
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
        print(f"Results for '{q}': {len(rows)}")
        for r in rows[:15]:
            print(f"[{r['idx']}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "Merchants"
    asyncio.run(run(query))
