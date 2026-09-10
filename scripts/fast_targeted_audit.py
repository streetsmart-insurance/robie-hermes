import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

sys.path.append('/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system')
from playwright.async_api import async_playwright
from src.ezlynx.session_manager import EZLynxSessionManager

async def audit_all():
    mgr = EZLynxSessionManager(cdp_url=None) # Use standalone with cached session state
    
    async with async_playwright() as p:
        browser, context = await mgr.get_authenticated_context(p, headless=True)
        page = await context.new_page()
        
        # ----------------------------------------------------
        # 1. EMPOWER GROUP LLC (69070440)
        # ----------------------------------------------------
        logging.info(">>> 1. AUDITING EMPOWER GROUP LLC (69070440)...")
        await page.goto("https://app.ezlynx.com/web/account/69070440/activity", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        
        # Policies API
        policies_data = await page.evaluate('''async () => {
            const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=69070440');
            return r.ok ? await r.json() : {};
        }''')
        cards = policies_data.get("policyCards", [])
        geico_card = next((c for c in cards if "9300334879" in c.get("policyNumber", "")), None)
        logging.info(f"Geico Policy Card: {geico_card}")
        
        # Discussions API
        discs_data = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=15&applicantId=69070440&applicantContext=true');
            return r.ok ? await r.json() : {};
        }''')
        logging.info(f"Retrieved {len(discs_data.get('discussions', []))} discussions for Empower Group:")
        for d in discs_data.get("discussions", []):
            logging.info(f"  Discussion [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                logging.info(f"    Note: {note[:150].strip()}")
                
        # Documents page & download '2004 Chev Removed.pdf'
        logging.info("Checking documents on Empower Group...")
        await page.goto("https://app.ezlynx.com/web/account/69070440/documents", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_docs_view.png")
        
        # Locate 2004 Chev Removed.pdf row and trigger download or get file info
        doc_rows = await page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('mat-row, tr, [role="row"]'));
            return rows.map(r => ({
                text: r.innerText ? r.innerText.trim().replace(/\\n+/g, ' | ') : '',
                hasDownload: !!r.querySelector('button, a, [aria-label*="download" i]')
            })).filter(x => x.text.length > 5);
        }''')
        for dr in doc_rows:
            logging.info(f"  Doc row: {dr['text']}")
            
        # Inspect Geico Policy Detail
        if geico_card:
            pol_id = geico_card.get("policyId")
            logging.info(f"Navigating to Geico Policy Details (ID: {pol_id})...")
            await page.goto(f"https://app.ezlynx.com/web/account/69070440/policies/{pol_id}", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_geico_details.png")
            
            # Policy History
            await page.goto(f"https://app.ezlynx.com/web/account/69070440/policies/{pol_id}/history", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_geico_history.png")
            hist_items = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll('mat-row, tr, [role="row"], .history-item'));
                return rows.map(r => r.innerText ? r.innerText.trim().replace(/\\n+/g, ' | ') : '').filter(t => t.length > 5);
            }''')
            logging.info(f"Geico Policy History ({len(hist_items)} rows):")
            for h in hist_items:
                logging.info(f"  * {h}")

        # ----------------------------------------------------
        # 2. CARDONE ELECTRIC LLC (51287231)
        # ----------------------------------------------------
        logging.info("\n>>> 2. AUDITING CARDONE ELECTRIC LLC (51287231)...")
        await page.goto("https://app.ezlynx.com/web/account/51287231/activity", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        
        cardone_pol_data = await page.evaluate('''async () => {
            const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=51287231');
            return r.ok ? await r.json() : {};
        }''')
        c_cards = cardone_pol_data.get("policyCards", [])
        merch_card = next((c for c in c_cards if "CAPI075976" in c.get("policyNumber", "")), None)
        logging.info(f"Merchants Policy Card: {merch_card}")
        
        if merch_card:
            pol_id = merch_card.get("policyId")
            # Policy Details / Drivers
            await page.goto(f"https://app.ezlynx.com/web/account/51287231/policies/{pol_id}", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_merch_details.png")
            
            # Policy History / Transactions / IVANS download
            logging.info("Checking Cardone Merchants Policy History...")
            await page.goto(f"https://app.ezlynx.com/web/account/51287231/policies/{pol_id}/history", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_merch_history.png")
            
            cardone_hist = await page.evaluate('''() => {
                const rows = Array.from(document.querySelectorAll('mat-row, tr, [role="row"], .history-item'));
                return rows.map(r => r.innerText ? r.innerText.trim().replace(/\\n+/g, ' | ') : '').filter(t => t.length > 5);
            }''')
            logging.info(f"Cardone Policy History ({len(cardone_hist)} rows):")
            for h in cardone_hist:
                logging.info(f"  * {h}")

        # ----------------------------------------------------
        # 3. SEACREST SALES & MARKETING (217163055)
        # ----------------------------------------------------
        logging.info("\n>>> 3. AUDITING SEACREST SALES & MARKETING (217163055)...")
        await page.goto("https://app.ezlynx.com/web/account/217163055/activity", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        seacrest_discs = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=10&applicantId=217163055&applicantContext=true');
            return r.ok ? await r.json() : {};
        }''')
        for d in seacrest_discs.get("discussions", []):
            logging.info(f"  Discussion [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                logging.info(f"    Note: {note[:200].strip()}")

        # ----------------------------------------------------
        # 4. JOHN GUARINI (21587599)
        # ----------------------------------------------------
        logging.info("\n>>> 4. AUDITING JOHN GUARINI (21587599)...")
        await page.goto("https://app.ezlynx.com/web/account/21587599/activity", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        guarini_discs = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=10&applicantId=21587599&applicantContext=true');
            return r.ok ? await r.json() : {};
        }''')
        for d in guarini_discs.get("discussions", []):
            logging.info(f"  Discussion [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                logging.info(f"    Note: {note[:200].strip()}")

        # ----------------------------------------------------
        # 5. UNITED PAVING & MASONRY (108248067)
        # ----------------------------------------------------
        logging.info("\n>>> 5. AUDITING UNITED PAVING & MASONRY (108248067)...")
        await page.goto("https://app.ezlynx.com/web/account/108248067/activity", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        up_discs = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=10&applicantId=108248067&applicantContext=true');
            return r.ok ? await r.json() : {};
        }''')
        for d in up_discs.get("discussions", []):
            logging.info(f"  Discussion [{d.get('created', '')[:10]}] ID: {d.get('id')} | Title: {d.get('title')}")
            note = (d.get('discussionNote') or {}).get('note', '')
            if note:
                logging.info(f"    Note: {note[:200].strip()}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(audit_all())
