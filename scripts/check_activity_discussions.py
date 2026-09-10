import asyncio
from playwright.async_api import async_playwright

accounts = [
    ('150751441', 'Imperio Enterprises I LLC'),
    ('102351931', 'Advance Marble & Granite LLC'),
    ('157388228', 'Epoxy Concrete Coatings LLC'),
    ('151382204', 'Ank Construction LLC'),
    ('99055770', 'Le Shawn Sneed')
]

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = b.contexts[0]
        page = [pg for pg in ctx.pages if 'ezlynx.com' in pg.url][0]
        for app_id, name in accounts:
            url = f'https://app.ezlynx.com/web/account/{app_id}/activity'
            print(f'Navigating to {name} ({url})...', flush=True)
            await page.goto(url, wait_until='domcontentloaded')
            await page.wait_for_timeout(3500)
            discussions = await page.evaluate('''() => {
                const cards = Array.from(document.querySelectorAll('.activity-container'));
                return cards.map(c => {
                    const text = c.innerText.split('\\n')[0].trim();
                    const fullText = c.innerText;
                    return {
                        title: text,
                        hasAddButton: !!c.querySelector('button[title="Add to Discussion"]'),
                        hasWrench: !!c.querySelector('button[title*="Edit"], mat-icon')
                    };
                });
            }''')
            print(f'=== Discussions for {name} ({len(discussions)} found) ===', flush=True)
            for d in discussions:
                if 'renewal' in d['title'].lower() or d['hasAddButton']:
                    print(f"  * {d['title']} (AddBtn: {d['hasAddButton']})", flush=True)

asyncio.run(check())
