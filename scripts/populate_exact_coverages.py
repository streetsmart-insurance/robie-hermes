import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def fill_exact_vehicle_coverages() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        
        # Click Vehicles
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(1000)
        
        # Click Edit on Vehicle 1
        await page.evaluate("() => document.querySelector('a[data-original-title=\"Edit\"]').click()")
        await page.wait_for_timeout(1500)
        
        # Open Coverages accordion
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('.repeaterEntryModal.in button.collapsible'));
            const covBtn = btns.find(b => b.textContent.includes('Coverages'));
            if (covBtn) covBtn.click();
        }""")
        await page.wait_for_timeout(1000)
        
        # Populate Vehicle Premium and Deductibles
        await page.evaluate("""() => {
            const prem = document.getElementById('Vehicle_TotalPremiumAmount_A');
            if (prem) { prem.value = '2450'; prem.dispatchEvent(new Event('input', {bubbles: true})); prem.dispatchEvent(new Event('change', {bubbles: true})); }
            
            const comp = document.getElementById('Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A');
            if (comp) { comp.value = '1000'; comp.dispatchEvent(new Event('input', {bubbles: true})); comp.dispatchEvent(new Event('change', {bubbles: true})); }
            
            const coll = document.getElementById('Vehicle_Collision_DeductibleAmount_A');
            if (coll) { coll.value = '1000'; coll.dispatchEvent(new Event('input', {bubbles: true})); coll.dispatchEvent(new Event('change', {bubbles: true})); }
            
            const vtype = document.getElementById('Vehicle_VehicleType_A');
            if (vtype) { vtype.value = 'Van'; vtype.dispatchEvent(new Event('input', {bubbles: true})); vtype.dispatchEvent(new Event('change', {bubbles: true})); }
        }""")
        
        # Check select options for Liability dropdown
        liab_options = await page.evaluate("""() => {
            const sel = Array.from(document.querySelectorAll('.repeaterEntryModal.in select')).find(s => s.name && s.name.includes('LiabilityIndicator'));
            if (!sel) return [];
            return Array.from(sel.options).map(o => ({text: o.text, val: o.value}));
        }""")
        print("Liability select options:", liab_options)
        
        # Select first valid liability option (e.g. index 1 or CSL)
        await page.evaluate("""() => {
            const sel = Array.from(document.querySelectorAll('.repeaterEntryModal.in select')).find(s => s.name && s.name.includes('LiabilityIndicator'));
            if (sel && sel.options.length > 1) {
                sel.selectedIndex = 1;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""")
        
        # Save Vehicle Modal
        await page.locator(".repeaterEntryModal.in button.btn-primary:has-text('Save')").first.click()
        await page.wait_for_timeout(2000)
        
        # Save & Close FormEntry
        await page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first.click()
        await page.wait_for_timeout(3000)
        print("Saved and closed FormEntry.")


if __name__ == "__main__":
    asyncio.run(fill_exact_vehicle_coverages())
