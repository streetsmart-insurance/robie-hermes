import asyncio
import json
import os
import sys
from pathlib import Path

# Add paths
sys.path.append('/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system')
from playwright.async_api import async_playwright
from src.ezlynx.session_manager import EZLynxSessionManager

async def run_audit():
    mgr = EZLynxSessionManager()
    
    async with async_playwright() as p:
        browser, context = await mgr.get_authenticated_context(p, headless=True)
        page = await context.new_page()
        
        # ==========================================================
        # 1. EMPOWER GROUP LLC (69070440)
        # ==========================================================
        print("\n" + "="*50)
        print("1. EMPOWER GROUP LLC (69070440)")
        print("="*50)
        
        # Navigate to account overview
        await page.goto("https://app.ezlynx.com/web/account/69070440/activity", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
        # Get discussions
        discs = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=10&applicantId=69070440&applicantContext=true');
            if (!r.ok) return [];
            const j = await r.json();
            return j.discussions || [];
        }''')
        print(f"Discussions count: {len(discs)}")
        for d in discs:
            print(f"  * [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                print(f"    Note: {note[:120].strip()}")
                
        # Check Change Requests API or UI
        # Let's inspect /EZLynxPortalAPI or any change request endpoints
        change_requests = await page.evaluate('''async () => {
            try {
                // Try several common EZLynx endpoints for change requests
                const res = {};
                const r1 = await fetch('/EZLynxPortalAPI/Applicant/GetApplicantTasks?applicantId=69070440');
                if (r1.ok) res.tasks = await r1.json();
                
                const r2 = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=69070440');
                if (r2.ok) res.policies = await r2.json();
                
                return res;
            } catch (e) {
                return { error: e.message };
            }
        }''')
        print("Tasks / Policy Card keys:", list(change_requests.keys()))
        if "policies" in change_requests:
            for pc in change_requests["policies"].get("policyCards", []):
                print(f"  Policy: {pc.get('policyNumber')} | ID: {pc.get('policyId')} | LOB: {pc.get('lob')} | Carrier: {pc.get('carrierName')}")

        # Navigate to Geico Commercial Auto policy details (9300334879)
        geico_policy = next((p for p in change_requests.get("policies", {}).get("policyCards", []) if "9300334879" in p.get("policyNumber", "")), None)
        if geico_policy:
            pol_id = geico_policy.get("policyId")
            print(f"\nNavigating to Geico Commercial Auto Policy (ID: {pol_id})...")
            pol_url = f"https://app.ezlynx.com/web/account/69070440/policies/{pol_id}"
            await page.goto(pol_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_geico_pol_details.png")
            
            # Extract policy tabs / vehicle schedule / driver schedule
            pol_details = await page.evaluate('''() => {
                const text = document.body.innerText;
                const vehicles = [];
                // Find vehicle section
                const vEls = Array.from(document.querySelectorAll('mat-row, tr, [role="row"], .vehicle-card'));
                return {
                    textSnippet: text.substring(0, 1000).replace(/\\n+/g, ' | '),
                    rows: vEls.map(r => r.innerText.replace(/\\n+/g, ' | ').trim()).filter(t => t.length > 5).slice(0, 15)
                };
            }''')
            print("Policy Page Rows:", pol_details["rows"][:8])
            
        # Download documents for Empower Group
        print("\nChecking Documents in Empower Group Library...")
        await page.goto("https://app.ezlynx.com/web/account/69070440/documents", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
        # Find document list via internal API or DOM
        doc_api = await page.evaluate('''async () => {
            try {
                const r = await fetch('/EZLynxPortalAPI/Documents/GetPagedDocuments?applicantId=69070440&pageNumber=1&pageSize=50');
                if (r.ok) return await r.json();
                return { status: r.status };
            } catch(e) {
                return { error: e.message };
            }
        }''')
        print("Document API status/result:", type(doc_api), list(doc_api.keys()) if isinstance(doc_api, dict) else len(doc_api))
        
        # Save screenshot of documents page
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_docs_page.png")

        # ==========================================================
        # 2. CARDONE ELECTRIC LLC (51287231)
        # ==========================================================
        print("\n" + "="*50)
        print("2. CARDONE ELECTRIC LLC (51287231)")
        print("="*50)
        await page.goto("https://app.ezlynx.com/web/account/51287231/policies", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
        cardone_pol_api = await page.evaluate('''async () => {
            const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=51287231');
            if (r.ok) return await r.json();
            return {};
        }''')
        cards = cardone_pol_api.get("policyCards", [])
        print(f"Cardone Policies count: {len(cards)}")
        for c in cards:
            print(f"  Policy: {c.get('policyNumber')} | ID: {c.get('policyId')} | LOB: {c.get('lob')} | Carrier: {c.get('carrierName')}")
            
        # Check Merchants Policy History / Transactions
        merch_pol = next((p for p in cards if "CAPI075976" in p.get("policyNumber", "")), None)
        if merch_pol:
            pol_id = merch_pol.get("policyId")
            print(f"\nChecking Policy History for Cardone Merchants Policy {pol_id}...")
            # Check policy history URL
            hist_url = f"https://app.ezlynx.com/web/account/51287231/policies/{pol_id}/history"
            await page.goto(hist_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_merchants_history.png")
            
            history_rows = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll('table tr, mat-row, .mat-mdc-row, .history-row, [role="row"]'));
                return rows.map(r => r.innerText.replace(/\\n+/g, ' | ').trim()).filter(t => t.length > 5);
            }''')
            print(f"Cardone History rows ({len(history_rows)}):")
            for hr in history_rows:
                print("  * History:", hr)
                
            # Check drivers on this policy
            drivers_url = f"https://app.ezlynx.com/web/account/51287231/policies/{pol_id}"
            await page.goto(drivers_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_policy_details.png")
            drivers_rows = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll('table tr, mat-row, .mat-mdc-row, [role="row"]'));
                return rows.map(r => r.innerText.replace(/\\n+/g, ' | ').trim()).filter(t => t.length > 5);
            }''')
            print(f"Cardone Policy Details rows ({len(drivers_rows)}):")
            for dr in drivers_rows[:10]:
                print("  * Detail:", dr)

        # ==========================================================
        # 3. JOHN GUARINI (21587599 / 41600472)
        # ==========================================================
        print("\n" + "="*50)
        print("3. JOHN GUARINI (21587599 / 41600472)")
        print("="*50)
        for gid in ["21587599", "41600472"]:
            await page.goto(f"https://app.ezlynx.com/web/account/{gid}/activity", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)
            guarini_discs = await page.evaluate(f'''async () => {{
                const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=5&applicantId={gid}&applicantContext=true');
                if (r.ok) return await r.json();
                return {{}};
            }}''')
            print(f"Guarini ID {gid} discussions count: {len(guarini_discs.get('discussions', []))}")
            for d in guarini_discs.get("discussions", []):
                print(f"  * [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
                note = (d.get('discussionNote') or {}).get('note', '')
                if note:
                    print(f"    Note: {note[:120].strip()}")

        # ==========================================================
        # 4. UNITED PAVING & MASONRY (108248067)
        # ==========================================================
        print("\n" + "="*50)
        print("4. UNITED PAVING & MASONRY (108248067)")
        print("="*50)
        await page.goto("https://app.ezlynx.com/web/account/108248067/policies", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        up_pol_api = await page.evaluate('''async () => {
            const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=108248067');
            if (r.ok) return await r.json();
            return {};
        }''')
        for c in up_pol_api.get("policyCards", []):
            print(f"  Policy: {c.get('policyNumber')} | ID: {c.get('policyId')} | LOB: {c.get('lob')} | Carrier: {c.get('carrierName')}")

        await page.goto("https://app.ezlynx.com/web/account/108248067/activity", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        up_discs = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=5&applicantId=108248067&applicantContext=true');
            if (r.ok) return await r.json();
            return {};
        }''')
        print(f"United Paving discussions count: {len(up_discs.get('discussions', []))}")
        for d in up_discs.get("discussions", []):
            print(f"  * [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                print(f"    Note: {note[:120].strip()}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_audit())
