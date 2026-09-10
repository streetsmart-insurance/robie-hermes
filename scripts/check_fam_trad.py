import asyncio
import json
from playwright.async_api import async_playwright

async def get_doc():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(storage_state="/opt/renewal-automation-system/data/ezlynx_storage_state.json")
        page = await ctx.new_page()
        
        # 1. Check EZLynx account policies
        await page.goto("https://app.ezlynx.com/web/account/27904633/policies", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        cards = await page.evaluate("""async () => {
            const res = await fetch("/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=27904633");
            return res.ok ? await res.json() : null;
        }""")
        if cards and "policyCards" in cards:
            for c in cards["policyCards"]:
                if "capi082129" in c.get("policyNumber", "").lower():
                    print("FAMILY TRADITION POLICY CARD:")
                    print("Policy:", c.get("policyNumber"))
                    print("LOB:", c.get("lob"))
                    print("Carrier:", c.get("carrierName"))
                    print("PendingCR:", c.get("hasPendingChangeRequest"))
                    print("EffDate:", c.get("policyChangeRequestEffectiveDate"))
                    print("Desc:", c.get("description"))
                    print("FullTermPremium:", c.get("fullTermPremium"))
                    print("Premium:", c.get("premium"))
        
        # 2. Check documents
        await page.goto("https://app.ezlynx.com/web/account/27904633/documents", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        docs = await page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tr, .document-row, [class*="document-grid"] tr'));
            return rows.map(r => r.innerText.replace(/[\\n\\r]+/g, ' | ')).filter(t => t.includes('CAPI082129') || t.includes('PolicyChangeRequest'));
        }""")
        print("\nFAMILY TRADITION MATCHING DOCS:")
        for d in docs:
            print("  ", d)
            
        await b.close()

if __name__ == "__main__":
    asyncio.run(get_doc())
