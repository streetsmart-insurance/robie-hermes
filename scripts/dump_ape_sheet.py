import asyncio
from playwright.async_api import async_playwright
import json

tabs = [
    {"name": "APE- Personal Lines Policy", "gid": "382713725"},
    {"name": "APE - Commercial Lines Policy", "gid": "912248473"},
    {"name": "APE - Trucking Lines Policy", "gid": "1796515433"},
    {"name": "Generic LOB", "gid": "564619717"}
]

async def dump_all():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        results = {}
        for t in tabs:
            url = f"https://docs.google.com/spreadsheets/d/1OTrvGJR9GlzH9b_JBkvpIVT-XZ_UQuUcIwYWtc3Bs-A/htmlview/sheet?gid={t['gid']}"
            print(f"\n=================== TAB: {t['name']} (gid={t['gid']}) ===================")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            data = await page.evaluate('''() => {
                const trs = Array.from(document.querySelectorAll("table.waffle tr, table tr"));
                const rows = [];
                for (let tr of trs) {
                    const cells = Array.from(tr.querySelectorAll("td, th"));
                    const rowVals = cells.map(c => {
                        const a = c.querySelector("a");
                        const text = c.innerText.trim();
                        if (a && a.href) {
                            return `${text} [LINK: ${a.href}]`;
                        }
                        return text;
                    }).filter(Boolean);
                    if (rowVals.length > 0) {
                        rows.push(rowVals);
                    }
                }
                return rows;
            }''')
            
            print(f"Extracted {len(data)} rows.")
            for i, r in enumerate(data):
                print(f"Row {i+1}: {' | '.join(r)}")
            results[t['name']] = data
            
        await page.close()
        return results

if __name__ == "__main__":
    res = asyncio.run(dump_all())
    with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/ape_sheet_dump.json", "w") as f:
        json.dump(res, f, indent=2)
    print("\nSaved all tab data to data/ape_sheet_dump.json")
