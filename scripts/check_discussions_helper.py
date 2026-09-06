import asyncio
from playwright.async_api import async_playwright

async def check_discussions():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = [pg for pg in ctx.pages if "108248067" in pg.url.lower()][0]
        await page.goto("https://app.ezlynx.com/web/account/108248067/discussions", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        print("Discussions URL:", page.url)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/united_paving_discussions.png")
        
        cards = await page.evaluate('''() => {
            const elms = Array.from(document.querySelectorAll(".discussion-item, .mat-mdc-card, .discussion-card, [role='article'], .card, tr, mat-row"));
            return elms.map(e => e.innerText ? e.innerText.trim().replace(/[\\r\\n]+/g, ' | ') : '').filter(t => t.length > 10);
        }''')
        print(f"Cards found ({len(cards)}):")
        for c in cards[:10]:
            print("  *", c[:300])

if __name__ == "__main__":
    asyncio.run(check_discussions())
