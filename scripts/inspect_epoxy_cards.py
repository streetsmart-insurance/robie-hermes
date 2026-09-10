import asyncio
from playwright.async_api import async_playwright

async def inspect():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = b.contexts[0]
        page = [pg for pg in ctx.pages if 'ezlynx.com' in pg.url][0]
        await page.goto('https://app.ezlynx.com/web/account/157388228/activity', wait_until='domcontentloaded')
        await asyncio.sleep(4)
        texts = await page.evaluate('''() => {
            const cards = Array.from(document.querySelectorAll('.activity-container'));
            return cards.map(c => {
                const lines = c.innerText.split('\\n').map(l => l.trim()).filter(Boolean);
                return lines.slice(0, 4).join(' | ');
            });
        }''')
        for idx, t in enumerate(texts):
            print(f'Card {idx:2d}: {t}')

asyncio.run(inspect())
