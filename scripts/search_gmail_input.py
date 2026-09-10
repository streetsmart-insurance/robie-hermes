import asyncio
from playwright.async_api import async_playwright

async def search(query):
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        # Find search box
        sb = page.locator('input[aria-label="Search mail"], input[name="q"]').first
        await sb.click()
        await sb.fill("")
        await sb.fill(query)
        await sb.press("Enter")
        await asyncio.sleep(4)
        
        await page.screenshot(path=f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/gmail_search_{query[:10]}.png")
        
        # Get results
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
        
        print(f"Query: '{query}' -> Results: {len(rows)}")
        for r in rows[:10]:
            print(f"[{r['idx']}] {r['sender']} | {r['subject']} | {r['snippet']} | {r['date']}")

if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "merchantsgroup"
    asyncio.run(search(q))
