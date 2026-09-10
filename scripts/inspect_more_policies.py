import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def inspect_more():
    storage_state = "/opt/renewal-automation-system/data/ezlynx_storage_state.json"
    targets = [
        {"app_id": "41600472", "pol": "04283052", "name": "John Guarini"},
        {"app_id": "72885007", "pol": "OLF-0002713", "name": "James Santiago"},
        {"app_id": "69976738", "pol": "CAPI074568", "name": "KJSD Enterprises Inc"},
        {"app_id": "217163055", "pol": "EZXS3251600", "name": "Seacrest Sales & Marketing Corporation"}
    ]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state)
        page = await context.new_page()

        for t in targets:
            acc_id = t["app_id"]
            pol_num = t["pol"]
            name = t["name"]
            print(f"\n=======================================================")
            print(f"  ACCOUNT {acc_id} | {name} | POLICY {pol_num}")
            print(f"=======================================================")
            
            await page.goto(f"https://app.ezlynx.com/web/account/{acc_id}/policies", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            cards = await page.evaluate("""async (idVal) => {
                try {
                    const res = await fetch(`/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=${idVal}`);
                    return res.ok ? await res.json() : null;
                } catch(e) { return null; }
            }""", acc_id)
            
            if cards and isinstance(cards, dict) and "policyCards" in cards:
                for c in cards["policyCards"]:
                    c_pol = c.get("policyNumber", "")
                    if pol_num.lower() in c_pol.lower():
                        c_lob = c.get("lob", "")
                        c_carr = c.get("carrierName", "")
                        c_pcr = c.get("hasPendingChangeRequest")
                        c_eff = c.get("policyChangeRequestEffectiveDate", "")
                        c_desc = c.get("description", "")
                        print(f"Policy: {c_pol} | LOB: {c_lob} | Carrier: {c_carr}")
                        print(f"  PendingCR: {c_pcr} | EffDate: {c_eff}")
                        print(f"  Desc: {c_desc}")
            
            # Docs
            await page.goto(f"https://app.ezlynx.com/web/account/{acc_id}/documents", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            doc_rows = await page.evaluate("""() => {
                const rows = Array.from(document.querySelectorAll('tr, .document-row, [class*="document-grid"] tr'));
                return rows.map(r => r.innerText.replace(/\\n+/g, ' | ')).filter(t => t.trim().length > 0 && !t.includes('Document Name'));
            }""")
            print(f"Recent Documents ({len(doc_rows)} found):")
            for dr in doc_rows[:6]:
                print(f"  [DOC] {dr[:120]}")
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_more())
