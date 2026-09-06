import asyncio
from playwright.async_api import async_playwright
import json

sheet_id = "1OTrvGJR9GlzH9b_JBkvpIVT-XZ_UQuUcIwYWtc3Bs-A"

tabs = [
    {"name": "Personal Lines Policy", "gid": "382713725"},
    {"name": "Commercial Lines Policy", "gid": "912248473"},
    {"name": "Trucking Lines Policy", "gid": "1796515433"},
    {"name": "Generic LOB", "gid": "564619717"}
]

async def extract_links():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        all_tabs_data = {}
        
        for t in tabs:
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={t['gid']}"
            print(f"\nNavigating to {t['name']} ({url})...")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            
            # Extract links and cell contents via JS
            # In Google sheets, rich text cells often have <a> tags or cell models
            tab_info = await page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll("a"));
                const relevantLinks = links.map(a => ({
                    text: a.innerText.trim(),
                    href: a.href
                })).filter(a => a.href && !a.href.includes("support.google.com") && !a.href.includes("accounts.google.com") && a.href !== window.location.href);
                
                return {
                    links: relevantLinks
                };
            }''')
            print(f"Found {len(tab_info['links'])} links in tab {t['name']}")
            for l in tab_info['links']:
                print(f"  * {l['text']} -> {l['href']}")
            all_tabs_data[t['name']] = tab_info
            
        await page.close()
        return all_tabs_data

if __name__ == "__main__":
    res = asyncio.run(extract_links())
    with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/sheet_extracted_links.json", "w") as f:
        json.dump(res, f, indent=2)
