import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        ez_page = ctx.pages[0]
        
        # Verify txtNote and txtDiscussionTitle are present
        has_note_box = await ez_page.evaluate("""() => {
            return {
                title: !!document.querySelector('#txtDiscussionTitle'),
                note: !!document.querySelector('#txtNote'),
                btn: !!document.querySelector('#btnSaveNote')
            };
        }""")
        print("Note box elements:", has_note_box)
        
        if not has_note_box['note']:
            print("Note box not open, clicking Add to Discussion...")
            await ez_page.evaluate("""() => {
                const h5 = Array.from(document.querySelectorAll('h5.discussion-title')).find(h => h.innerText.includes('CHANGE ME') || h.innerText.includes('2005 Ford'));
                if (h5) {
                    const btn = h5.querySelector('button[title="Add to Discussion"]');
                    if (btn) btn.click();
                }
            }""")
            await asyncio.sleep(2)
            
        new_title = "Personal Auto Policy Change Request - Add 2005 Ford Econoline E350 Super Duty Wagon, effective 09/08/2026, liability only."
        note_text = """Policy change endorsement submitted and bound directly on National General (Integon National) online portal:
- Policy Number: 203193685900
- Endorsement: Quote #12 officially loaded & confirmed effective 09/08/2026.
- Added Vehicle: Unit 7 - 2005 Ford Econoline E350 Super Duty Wagon, VIN: 1FBSS31L05HB40186.
- Coverages Configured: Liability Only (Bodily Injury $100k/$300k, Property Damage $50k, NJ PIP $250k w/ $250 ded; No Comp, No Coll, No Towing, No Rental).
- Garaging: 55 Ford Rd, Howell, NJ 07731-2416 (Same garaging).
- Registered Owner: Jesus Solano. No new drivers, local pleasure/commute use.
- Financials & Billing:
  * Pro-rated Additional Premium: +$227.28 ($208.00 premium + $19.28 fees).
  * Full Term Revised Premium: $12,140.31 (from $10,686.03).
  * Billing: Direct Bill with National General.
- EZLynx Change Request: Keyed into policy history effective 09/08/2026 with hasPendingChangeRequest=True.
- Status: Awaiting carrier IVANS electronic download (PCH) for reconciliation. Handed off to CSR (Ana Flores) for confirmation upon download arrival."""

        print("Setting title...")
        await ez_page.fill('#txtDiscussionTitle', new_title)
        print("Setting note text...")
        await ez_page.fill('#txtNote', note_text)
        
        await ez_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/ezlynx_note_filled.png')
        print("Saved ezlynx_note_filled.png")
        
        print("Clicking #btnSaveNote...")
        await ez_page.click('#btnSaveNote')
        await asyncio.sleep(4)
        
        await ez_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/ezlynx_note_saved.png')
        print("Saved ezlynx_note_saved.png")
        
        # Verify discussion content
        text = await ez_page.evaluate("""() => {
            const h5 = Array.from(document.querySelectorAll('h5.discussion-title')).find(h => h.innerText.includes('2005 Ford') || h.innerText.includes('CHANGE ME'));
            if (!h5) return 'h5 not found after save';
            const container = h5.closest('.activity-container') || h5.parentElement;
            return container ? container.innerText : 'container not found';
        }""")
        print("Discussion text after save:\n", text)

asyncio.run(main())
