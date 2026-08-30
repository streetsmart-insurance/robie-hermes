import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def inspect_cov_section() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Click Coverages nav
        await page.locator("a:has-text('Coverages'), span:has-text('Coverages')").first.click()
        await page.wait_for_timeout(1000)

        cov_fields = await page.locator("input, select").evaluate_all("""
            els => els.map(e => ({
                id: e.id,
                name: e.name,
                type: e.type,
                ngModel: e.getAttribute('ng-model'),
                value: e.value
            })).filter(e => e.id || e.name || e.ngModel)
        """)
        print(f"Coverages fields ({len(cov_fields)}):")
        for f in cov_fields:
            if "Coverage" in str(f['id']) or "Coverage" in str(f['name']) or "Limit" in str(f['id']) or "Limit" in str(f['name']) or "Deductible" in str(f['id']):
                print(f"  {f}")


if __name__ == "__main__":
    asyncio.run(inspect_cov_section())
