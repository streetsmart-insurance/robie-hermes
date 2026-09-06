import asyncio
from playwright.async_api import async_playwright
import json

targets = [
    {"name": "SAPP Construction", "app_id": "19572458"},
    {"name": "John Guarini", "app_id": "15482910"},
    {"name": "Ferrara Organization", "app_id": "22104921"},
    {"name": "Empower Group", "app_id": "18920144"},
    {"name": "Seacrest Sales", "app_id": "17829104"}
]

async def check_docs():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = ctx.pages[0]
        
        doc_results = {}
        for t in targets:
            url = f"https://app.ezlynx.com/web/account/{t['app_id']}/documents"
            print(f"\nChecking documents for {t['name']} ({url})...")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(3.5)
            
            docs = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll("table tr, mat-row, .mat-mdc-row"));
                return rows.map(r => r.innerText ? r.innerText.trim().replace(/[\\r\\n]+/g, " | ") : "").filter(t => t.length > 5);
            }''')
            
            screenshot_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/{t['name'].lower().replace(' ', '_')}_docs.png"
            await page.screenshot(path=screenshot_path)
            print(f"Captured {len(docs)} document rows. Saved screenshot to {screenshot_path}")
            for d in docs[:5]:
                print("  *", d)
            doc_results[t['name']] = {"rows": docs[:10], "screenshot": screenshot_path}
            
        return doc_results

if __name__ == "__main__":
    res = asyncio.run(check_docs())
    with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/p3_document_check_summary.json", "w") as f:
        json.dump(res, f, indent=2)
    print("\nSaved doc summary to data/p3_document_check_summary.json")
