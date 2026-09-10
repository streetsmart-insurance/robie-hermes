import asyncio
import json
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright

STORAGE_STATE = "/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system/data/ezlynx_storage_state.json"

async def inspect_account(app_id: str, name: str):
    print(f"\n==========================================")
    print(f"Inspecting: {name} (Applicant ID: {app_id})")
    print(f"==========================================")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=STORAGE_STATE,
            viewport={"width": 1440, "height": 900}
        )
        page = await ctx.new_page()
        
        # 1. Policies Tab
        policies_url = f"https://app.ezlynx.com/web/account/{app_id}/policies"
        print(f"Navigating to {policies_url}...")
        await page.goto(policies_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        
        # Take screenshot of policies
        screenshot_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/{app_id}_policies.png"
        await page.screenshot(path=screenshot_path)
        print(f"Screenshot saved to {screenshot_path}")
        
        policy_cards = await page.evaluate('''() => {
            const items = [];
            document.querySelectorAll('mat-card, .policy-card, tr, [role="row"]').forEach(el => {
                const text = el.innerText ? el.innerText.trim() : '';
                if (text.length > 10) {
                    items.push(text.split('\\n').map(s => s.trim()).filter(Boolean).join(' | '));
                }
            });
            return items;
        }''')
        print(f"Found {len(policy_cards)} policy card/table items:")
        for card in policy_cards[:8]:
            print("  *", card)
            
        # Extract all links that contain policy IDs
        policy_links = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('a[href*="/policies/"]')).map(a => ({
                href: a.href,
                text: a.innerText.trim()
            }));
        }''')
        print(f"Policy links ({len(policy_links)}):", json.dumps(policy_links, indent=2))
        
        # 2. Discussions / Tasks / Policy Change Requests
        print(f"\nFetching discussions & change requests for {app_id}...")
        disc_data = await page.evaluate(f'''async () => {{
            try {{
                const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=15&applicantId={app_id}&applicantContext=true');
                if (!r.ok) return {{ error: r.status }};
                return await r.json();
            }} catch(e) {{
                return {{ error: e.message }};
            }}
        }}''')
        discussions = disc_data.get("discussions", [])
        print(f"Total discussions retrieved: {len(discussions)}")
        for i, d in enumerate(discussions):
            title = d.get("title", "")
            created = d.get("created", "")
            disc_type = d.get("type", "")
            id_val = d.get("id", "")
            parent_id = d.get("parentId", "")
            note = d.get("discussionNote", {}).get("note", "").strip()
            print(f"  [{i+1}] ID:{id_val} | Date:{created[:10]} | Type:{disc_type} | Title: {title}")
            if note:
                print(f"       Note: {note[:160]}...")
                
        # 3. Documents
        docs_url = f"https://app.ezlynx.com/web/account/{app_id}/documents"
        print(f"\nNavigating to documents: {docs_url}...")
        await page.goto(docs_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)
        doc_screenshot = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/{app_id}_docs_latest.png"
        await page.screenshot(path=doc_screenshot)
        print(f"Screenshot saved to {doc_screenshot}")
        
        doc_items = await page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('table tr, mat-row, .mat-mdc-row, .document-row'));
            return rows.map(r => {
                const text = r.innerText ? r.innerText.trim().split('\\n').map(s => s.trim()).filter(Boolean).join(' | ') : '';
                return { text };
            }).filter(d => d.text.length > 5);
        }''')
        print(f"Found {len(doc_items)} document items:")
        for item in doc_items[:10]:
            print("  *", item["text"])
            
        await browser.close()

if __name__ == "__main__":
    import sys
    app_id = sys.argv[1] if len(sys.argv) > 1 else "69070440"
    name = sys.argv[2] if len(sys.argv) > 2 else "Empower Group LLC"
    asyncio.run(inspect_account(app_id, name))
