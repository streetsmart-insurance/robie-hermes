import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def inspect_vehicle_cov_modal() -> None:
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
        
        # Open Coverages accordion on Vehicle modal
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('.repeaterEntryModal.in button.collapsible'));
            const covBtn = btns.find(b => b.textContent.includes('Coverages'));
            if (covBtn) covBtn.click();
        }""")
        await page.wait_for_timeout(1000)
        
        # Inspect inputs inside Coverages accordion
        cov_inputs = await page.evaluate("""() => {
            const modal = document.querySelector('.repeaterEntryModal.in');
            if (!modal) return [];
            const inputs = Array.from(modal.querySelectorAll('input, select'));
            return inputs.map(i => ({
                id: i.id,
                name: i.name,
                type: i.type,
                tag: i.tagName,
                value: i.value,
                checked: i.checked,
                outer: i.outerHTML.substring(0, 150)
            }));
        }""")
        print("Vehicle modal inputs count:", len(cov_inputs))
        for i, item in enumerate(cov_inputs):
            if any(k in item['name'].lower() or k in item['id'].lower() for k in ['coverage', 'liab', 'comp', 'coll', 'pip', 'med', 'um', 'deductible']):
                print(f"  Cov #{i}: id='{item['id']}', name='{item['name']}', type='{item['type']}', val='{item['value']}', checked={item['checked']}")


if __name__ == "__main__":
    asyncio.run(inspect_vehicle_cov_modal())
