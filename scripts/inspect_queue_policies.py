import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def inspect_account(page, applicant_id, policy_number, policy_master_id=None):
    print(f"\n=======================================================")
    print(f"  INSPECTING ACCOUNT {applicant_id} | POLICY {policy_number}")
    print(f"=======================================================")
    
    # 1. Overview / Account Name
    await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/overview", wait_until="domcontentloaded")
    await asyncio.sleep(2)
    title = await page.title()
    print(f"Title: {title}")
    
    # 2. Policies API
    await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/policies", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    policies = await page.evaluate("""async (accId) => {
        try {
            const res = await fetch(`/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=${accId}`);
            if (!res.ok) return { error: `status ${res.status}` };
            return await res.json();
        } catch(e) { return { error: String(e) }; }
    }""", applicant_id)
    print(f"Policies raw type: {type(policies)}")
    if isinstance(policies, str):
        try:
            policies = json.loads(policies)
        except Exception:
            print("Raw text:", policies[:200])
    if isinstance(policies, dict):
        if "policies" in policies:
            policies = policies["policies"]
        elif "data" in policies:
            policies = policies["data"]
        elif "error" in policies:
            print("API Error:", policies)
            policies = []
            
    if not isinstance(policies, list):
        print(f"Policies is not a list: {policies}")
        policies = []

    # DOM scraping fallback
    dom_pols = await page.evaluate("""() => {
        const cards = Array.from(document.querySelectorAll('.policy-card, .list-group-item, tr'));
        return cards.map(c => c.innerText.replace(/\\n+/g, ' | ')).filter(t => t.includes('Policy') || t.includes('Active') || t.includes('Pending'));
    }""")
    if dom_pols:
        print(f"DOM Policies ({len(dom_pols)} matches):")
        for dp in dom_pols[:6]:
            print(f"  [DOM-POL] {dp[:120]}")
    
    pmid = policy_master_id
    if policies:
        print("Policies:")
        for pol in policies:
            pnum = pol.get('policyNumber', '')
            lob = pol.get('lineOfBusiness', '')
            carr = pol.get('carrierName', '')
            stat = pol.get('policyStatus', '')
            p_id = pol.get('policyMasterID')
            pcr = pol.get('hasPendingChangeRequest')
            is_match = policy_number.strip().lower() in pnum.strip().lower()
            if is_match and not pmid:
                pmid = p_id
            print(f"  {'[*]' if is_match else '[-]'} Pol: {pnum} | LOB: {lob} | Carrier: {carr} | Status: {stat} | MasterID: {p_id} | PendingCR: {pcr}")
            
    # 3. Policy History / Transactions
    if pmid:
        history = await page.evaluate("""async (idVal) => {
            try {
                const res = await fetch(`/PolicyAPI/v1/PolicyDetail/GetPolicyHistory?policyMasterId=${idVal}`);
                return res.ok ? await res.json() : null;
            } catch(e) { return null; }
        }""", pmid)
        if history:
            print(f"\nPolicy History raw type: {type(history)}")
            if isinstance(history, str):
                try: history = json.loads(history)
                except: pass
            if isinstance(history, dict):
                history = history.get('policyHistory', history.get('history', history.get('data', [history])))
            if isinstance(history, list):
                print("Policy History / Transactions:")
                for h in history:
                    if isinstance(h, dict):
                        ttype = h.get('transactionType', '')
                        tdate = h.get('transactionDate', '')
                        eff = h.get('effectiveDate', '')
                        stat = h.get('status', '')
                        prem = h.get('writtenPremium', '')
                        src = h.get('source', '')
                        print(f"  -> Type: {ttype} | Date: {tdate} | Eff: {eff} | Stat: {stat} | Prem: {prem} | Src: {src}")
                
    # 4. Documents tab
    await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/documents", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    doc_rows = await page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('tr, .document-row, [class*="document-grid"] tr'));
        return rows.map(r => r.innerText.replace(/\\n+/g, ' | ')).filter(t => t.trim().length > 0 && !t.includes('Document Name'));
    }""")
    print(f"\nDocuments ({len(doc_rows)} found):")
    for dr in doc_rows[:12]:
        print(f"  [DOC] {dr[:120]}")

async def main():
    storage_state = "/opt/renewal-automation-system/data/ezlynx_storage_state.json"
    targets = [
        {"app_id": "21586424", "pol": "203193685900", "pmid": "74675771", "name": "Jesus Solano"},
        {"app_id": "199729236", "pol": "NPP1674285", "pmid": "74996912", "name": "Top Notch Tree Service LLC"},
        {"app_id": "38142343", "pol": "CTRI021809", "pmid": None, "name": "AME Plumbing LLC"},
        {"app_id": "21587240", "pol": "ART3000087030", "pmid": None, "name": "Arellano's Future Landscaping LLC"},
        {"app_id": "186530244", "pol": "ART3001773600", "pmid": None, "name": "A List Cleaning Service LLC"},
        {"app_id": "27904633", "pol": "CAPI082129", "pmid": None, "name": "Family Tradition Plumbing and Heating LLC"},
    ]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state)
        page = await context.new_page()
        
        for t in targets:
            try:
                await inspect_account(page, t["app_id"], t["pol"], t.get("pmid"))
            except Exception as e:
                print(f"Error inspecting {t['name']}: {e}")
                
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
