import asyncio
from playwright.async_api import async_playwright

async def dump_tab(gid, label):
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        url = f"https://docs.google.com/spreadsheets/d/1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo/htmlview/sheet?gid={gid}"
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Read table cells
        rows = await page.evaluate('''() => {
            const trs = Array.from(document.querySelectorAll('tr'));
            return trs.map(tr => Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim()).filter(Boolean));
        }''')
        print(f"=== {label} (gid={gid}) ===")
        for r in rows:
            line = " | ".join(r)
            if any(name in line.lower() for name in ['ashley', 'sandy', 'gabi', 'gabriela', 'jake', 'carlo']):
                print(line)
        await page.close()

async def main():
    await dump_tab("1699679594", "Street Smart Employees")
    await dump_tab("0", "Employees")

asyncio.run(main())
