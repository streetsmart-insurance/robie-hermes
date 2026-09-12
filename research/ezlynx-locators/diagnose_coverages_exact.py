import asyncio
import os
from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"

async def inspect_vehicle_modal_coverages() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        
        # Click Vehicles tab
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(1000)
        
        # Open Vehicle 1 edit modal via force click
        edit_link = page.locator("a[data-original-title='Edit'], a:has-text('Edit')").first
        await edit_link.click(force=True)
        await page.wait_for_timeout(1500)
        
        # Open Coverages accordion
        cov_btn = page.locator(".repeaterEntryModal.in button.collapsible:has-text('Coverages'), .modal button.collapsible:has-text('Coverages')")
        if await cov_btn.count() > 0:
            await cov_btn.click()
            await page.wait_for_timeout(500)
            
        # Dump inputs inside modal
        modal_inputs = await page.evaluate("""() => {
            const modal = document.querySelector('.repeaterEntryModal.in, .modal');
            if (!modal) return [];
            return Array.from(modal.querySelectorAll('input, select')).map(el => ({
                id: el.id,
                name: el.name,
                type: el.type,
                value: el.value,
                checked: el.checked
            })).filter(el => el.id && el.id.length > 0);
        }""")
        print("=== VEHICLE MODAL INPUTS (COUNT: {}) ===".format(len(modal_inputs)))
        for inp in modal_inputs:
            if any(k in inp['id'].lower() for k in ['cover', 'deduct', 'liab', 'apply', 'prem', 'comp', 'coll', 'med', 'pip', 'motor']):
                print(f"Vehicle Modal Cov Field: id='{inp['id']}', name='{inp['name']}', val='{inp['value']}', checked={inp.get('checked')}")

if __name__ == "__main__":
    asyncio.run(inspect_vehicle_modal_coverages())
