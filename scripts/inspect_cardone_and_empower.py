import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.path.append('/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system')
from playwright.async_api import async_playwright
from src.ezlynx.session_manager import EZLynxSessionManager

async def main():
    mgr = EZLynxSessionManager(cdp_url=None)
    async with async_playwright() as p:
        browser, ctx = await mgr.get_authenticated_context(p, headless=True)
        page = await ctx.new_page()

        # ==========================================
        # CARDONE ELECTRIC: CAPI075976
        # ==========================================
        logging.info("=== CARDONE ELECTRIC: CAPI075976 ===")
        await page.goto("https://app.ezlynx.com/web/account/51287231/policies", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_policies_full.png", full_page=True)

        # Inspect Policy Card elements and change request options
        cardone_info = await page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('mat-card, .policy-card, .mat-mdc-card'));
            const res = [];
            for (let c of cards) {
                if (c.innerText.includes('CAPI075976')) {
                    const text = c.innerText;
                    const links = Array.from(c.querySelectorAll('a')).map(a => ({href: a.href, text: a.innerText.trim()}));
                    const buttons = Array.from(c.querySelectorAll('button')).map(b => b.innerText.trim() || b.getAttribute('aria-label') || b.className);
                    res.push({text, links, buttons});
                }
            }
            return res;
        }""")
        logging.info(f"Cardone CAPI075976 Card: {json.dumps(cardone_info, indent=2)}")

        # Check API for Policy Change Requests or Transactions on CAPI075976 (policyMasterID: 31887060)
        policy_api_res = await page.evaluate("""async () => {
            const results = {};
            // Try fetching policy change requests
            const urls = [
                '/PolicyAPI/v1/PolicyCard/GetPolicyDetail?policyMasterId=31887060',
                '/PolicyAPI/v1/PolicyChangeRequest/GetPolicyChangeRequests?policyMasterId=31887060',
                '/PolicyAPI/v1/PolicyHistory/GetPolicyHistory?policyMasterId=31887060',
                '/PolicyAPI/v1/Policy/GetPolicyTransactions?policyMasterId=31887060',
                '/EZLynxPortalAPI/PolicyChange/GetPolicyChangeRequests?applicantId=51287231'
            ];
            for (let u of urls) {
                try {
                    const r = await fetch(u);
                    results[u] = { status: r.status, ok: r.ok, data: r.ok ? await r.json() : null };
                } catch(e) {
                    results[u] = { error: e.message };
                }
            }
            return results;
        }""")
        logging.info(f"Cardone Policy API results keys: {list(policy_api_res.keys())}")
        for u, v in policy_api_res.items():
            logging.info(f"  Endpoint {u} -> Status: {v.get('status')} | OK: {v.get('ok')}")
            if v.get('ok') and v.get('data'):
                logging.info(f"    Data snippet: {json.dumps(v['data'])[:300]}")

        # ==========================================
        # EMPOWER GROUP: GEICO AUTO 9300334879
        # ==========================================
        logging.info("\n=== EMPOWER GROUP: GEICO AUTO 9300334879 ===")
        await page.goto("https://app.ezlynx.com/web/account/69070440/policies", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_policies_full.png", full_page=True)

        empower_cards = await page.evaluate("""async () => {
            const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=69070440');
            return r.ok ? await r.json() : {};
        }""")
        geico_master_id = None
        for c in empower_cards.get("policyCards", []):
            if "9300334879" in c.get("policyNumber", ""):
                geico_master_id = c.get("policyMasterID")
                logging.info(f"Found Geico policyMasterID: {geico_master_id}")
                logging.info(f"  Geico Card: {json.dumps(c, indent=2)}")

        if geico_master_id:
            empower_policy_api = await page.evaluate(f"""async () => {{
                const results = {{}};
                const urls = [
                    '/PolicyAPI/v1/PolicyCard/GetPolicyDetail?policyMasterId={geico_master_id}',
                    '/PolicyAPI/v1/PolicyChangeRequest/GetPolicyChangeRequests?policyMasterId={geico_master_id}',
                    '/PolicyAPI/v1/PolicyHistory/GetPolicyHistory?policyMasterId={geico_master_id}',
                    '/EZLynxPortalAPI/PolicyChange/GetPolicyChangeRequests?applicantId=69070440'
                ];
                for (let u of urls) {{
                    try {{
                        const r = await fetch(u);
                        results[u] = {{ status: r.status, ok: r.ok, data: r.ok ? await r.json() : null }};
                    }} catch(e) {{
                        results[u] = {{ error: e.message }};
                    }}
                }}
                return results;
            }}""")
            for u, v in empower_policy_api.items():
                logging.info(f"  Endpoint {u} -> Status: {v.get('status')} | OK: {v.get('ok')}")
                if v.get('ok') and v.get('data'):
                    logging.info(f"    Data: {json.dumps(v['data'])[:400]}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
