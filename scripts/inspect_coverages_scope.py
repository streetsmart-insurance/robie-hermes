import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def inspect_coverages_scope() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Inspect formData in FormEntry
        cov_info = await page.evaluate("""() => {
            const form = document.querySelector('#policyControllerApp, form[name="policyForm"]');
            if (!form) return "No form";
            const scope = angular.element(form).scope();
            if (!scope || !scope.formData) return "No formData";

            return {
                f127_keys: Object.keys(scope.formData.Form127 || {}).filter(k => k.toLowerCase().includes('comp') || k.toLowerCase().includes('liab') || k.toLowerCase().includes('cov')),
                f137_keys: Object.keys(scope.formData.Form137 || {}).filter(k => k.toLowerCase().includes('limit') || k.toLowerCase().includes('csl') || k.toLowerCase().includes('liab') || k.toLowerCase().includes('comp'))
            };
        }""")
        print("COVERAGE SCOPE KEYS:")
        print(cov_info)


if __name__ == "__main__":
    asyncio.run(inspect_coverages_scope())
