import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def save_acord_direct() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        res = await page.evaluate("""() => {
            const scope = angular.element(document.querySelector('#acordFormEntry')).scope();
            if (!scope || !scope.formData) return "No scope.formData";

            // Inspect keys
            const keys127 = Object.keys(scope.formData.Form127 || {});
            const keys137 = Object.keys(scope.formData.Form137 || {});

            // 1. Vehicle Coverages (Form 127)
            if (scope.formData.Form127) {
                const f = scope.formData.Form127;
                f.Vehicle_Coverage_CombinedSingleLimitAmount_A = "1000000";
                f.Vehicle_Coverage_LiabilityIndicator_A = "true";
                f.Vehicle_Coverage_ComprehensiveDeductibleIndicator_A = "true";
                f.Vehicle_Comprehensive_DeductibleAmount_A = "1000";
                f.Vehicle_Coverage_CollisionIndicator_A = "true";
                f.Vehicle_Collision_DeductibleAmount_A = "1000";
                f.Vehicle_VehicleType_A = "Commercial";
            }

            // 2. Policy Coverages (Form 137)
            if (scope.formData.Form137) {
                const f137 = scope.formData.Form137;
                f137.Coverage_CombinedSingleLimit_Amount = "1000000";
                f137.Coverage_Liability_CombinedSingleLimitIndicator = "On";
            }

            scope.$apply();

            // Call save
            scope.saveAcordFormData();
            return { keys127: keys127.length, keys137: keys137.length, saved: true };
        }""")
        print("SAVE RESULT:", res)
        await page.wait_for_timeout(4000)

        # Go to summary
        await page.goto("https://app.ezlynx.com/applicantportal/Policy/83280714/summary/index", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)


if __name__ == "__main__":
    asyncio.run(save_acord_direct())
