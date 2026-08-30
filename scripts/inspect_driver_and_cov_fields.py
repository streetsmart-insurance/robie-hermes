import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def inspect_driver_and_cov_fields() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # 1. Inspect Drivers modal
        await page.locator("a:has-text('Drivers'), span:has-text('Drivers')").first.click()
        await page.wait_for_timeout(1000)

        # Click Edit on Driver 1
        await page.evaluate("""() => {
            const editBtn = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (editBtn) editBtn.click();
        }""")
        await page.wait_for_timeout(1000)

        driver_inputs = await page.locator(".repeaterEntryModal.in input, .repeaterEntryModal.in select").evaluate_all("""
            els => els.map(e => ({
                id: e.id,
                name: e.name,
                type: e.type,
                ngModel: e.getAttribute('ng-model'),
                value: e.value
            }))
        """)
        print("Driver modal inputs:")
        for inp in driver_inputs:
            if inp['id'] or inp['name'] or inp['ngModel']:
                print(f"  {inp}")

        # Close Driver modal
        await page.evaluate("""() => {
            const cancel = document.querySelector(".repeaterEntryModal.in button:has-text('Cancel'), .repeaterEntryModal.in button.btn:not(.btn-primary)");
            if (cancel) cancel.click();
        }""")
        await page.wait_for_timeout(1000)

        # 2. Inspect Coverages section
        await page.locator("a:has-text('Coverages'), span:has-text('Coverages')").first.click()
        await page.wait_for_timeout(1000)

        cov_inputs = await page.locator("input, select").evaluate_all("""
            els => els.map(e => ({
                id: e.id,
                name: e.name,
                ngModel: e.getAttribute('ng-model'),
                placeholder: e.placeholder,
                value: e.value
            })).filter(e => e.id || e.name || e.ngModel)
        """)
        print(f"Coverages inputs found ({len(cov_inputs)}):")
        for c in cov_inputs[:30]:
            print(f"  {c}")


if __name__ == "__main__":
    asyncio.run(inspect_driver_and_cov_fields())
