import asyncio
from playwright.async_api import async_playwright

accounts_to_check = [
    {"name": "United Paving & Masonry", "query": "United Paving"},
    {"name": "SAPP Construction", "query": "SAPP Construction"},
    {"name": "John Guarini", "query": "John Guarini"},
    {"name": "Ferrara Organization", "query": "Ferrara Organization"},
    {"name": "Empower Group", "query": "Empower Group"},
    {"name": "Seacrest Sales & Marketing", "query": "Seacrest Sales"}
]

async def check_accounts():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        results = []
        for acc in accounts_to_check:
            print(f"\n==========================================")
            print(f"Checking: {acc['name']}")
            print(f"==========================================")
            search_url = f"https://app.ezlynx.com/web/search?query={acc['query']}"
            await page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            
            # Find applicant link
            links = await page.evaluate('''() => {
                const anchors = Array.from(document.querySelectorAll("a[href*='/web/applicant/']"));
                return anchors.map(a => ({ text: a.innerText.trim(), href: a.href }));
            }''')
            
            if not links:
                print("No search results found on search page.")
                results.append({"name": acc["name"], "status": "Not found in search", "docs": []})
                continue
                
            # Filter for applicant link (not sublink)
            app_links = [l for l in links if '/web/applicant/' in l['href'] and ('/overview' in l['href'] or l['href'].split('/web/applicant/')[1].split('/')[0].isdigit())]
            target_link = app_links[0] if app_links else links[0]
            print(f"Found applicant: {target_link['text']} -> {target_link['href']}")
            
            # Extract applicant ID
            parts = target_link['href'].split('/web/applicant/')[1].split('/')
            app_id = parts[0].split('?')[0]
            
            # Navigate to applicant documents
            docs_url = f"https://app.ezlynx.com/web/applicant/{app_id}/documents"
            print(f"Navigating to Documents: {docs_url}")
            await page.goto(docs_url, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            
            # Capture screenshot
            clean_name = acc['name'].lower().replace(' ', '_').replace('&', 'and')
            screenshot_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/{clean_name}_docs.png"
            await page.screenshot(path=screenshot_path)
            
            # Extract document list
            docs = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll("table tr, mat-row, .mat-mdc-row, .document-row, tr[role='row']"));
                return rows.map(r => r.innerText ? r.innerText.trim().replace(/[\\r\\n]+/g, ' | ') : '').filter(t => t.length > 5);
            }''')
            
            print(f"Documents listed ({len(docs)}):")
            recent_docs = []
            for d in docs[:6]:
                print(f"  * {d}")
                recent_docs.append(d)
                
            results.append({
                "name": acc["name"],
                "app_id": app_id,
                "screenshot": screenshot_path,
                "docs": recent_docs
            })
            
        print("\nAll accounts checked!")
        return results

if __name__ == "__main__":
    asyncio.run(check_accounts())
