import asyncio
from playwright.async_api import async_playwright
import json

targets = [
    {"name": "SAPP Construction Corp", "app_id": "41055091"},
    {"name": "John Guarini", "app_id": "21587599"},
    {"name": "Ferrara Organization LLC", "app_id": "211475390"},
    {"name": "Empower Group LLC", "app_id": "69070440"},
    {"name": "Seacrest Sales & Marketing", "app_id": "217163055"}
]

async def check_real_docs():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = ctx.pages[0]
        
        doc_results = {}
        for t in targets:
            url = f"https://app.ezlynx.com/web/account/{t['app_id']}/documents"
            print(f"\nChecking documents for {t['name']} ({url})...")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            
            docs = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll("table tr, mat-row, .mat-mdc-row"));
                return rows.map(r => r.innerText ? r.innerText.trim().replace(/[\\r\\n]+/g, " | ") : "").filter(t => t.length > 5);
            }''')
            
            screenshot_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/real_{t['name'].lower().replace(' ', '_')[:15]}_docs.png"
            await page.screenshot(path=screenshot_path)
            print(f"Captured {len(docs)} document rows. Saved screenshot to {screenshot_path}")
            for d in docs[:6]:
                print("  *", d)
            doc_results[t['name']] = {"rows": docs[:10], "screenshot": screenshot_path}
            
        return doc_results

if __name__ == "__main__":
    res = asyncio.run(check_real_docs())
    with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/p3_real_document_summary.json", "w") as f:
        json.dump(res, f, indent=2)
    print("\nSaved real doc summary to data/p3_real_document_summary.json")
