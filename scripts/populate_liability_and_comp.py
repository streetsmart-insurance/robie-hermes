import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def set_liability_and_comp() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Set Coverages in Angular scope directly
        await page.evaluate("""() => {
            const form = document.querySelector('#policyControllerApp, form[name="policyForm"]');
            if (!form) return;
            const scope = angular.element(form).scope();
            if (!scope) return;

            // 1. Vehicle level coverages
            if (scope.formData && scope.formData.Form127) {
                const f127 = scope.formData.Form127;
                f127.Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A = "1000";
                f127.Vehicle_Collision_DeductibleAmount_A = "1000";
                f127.Vehicle_Coverage_ComprehensiveDeductibleIndicator_A = "On";
                f127.Vehicle_Coverage_CollisionIndicator_A = "On";
                f127.Vehicle_Coverage_LiabilityIndicator_A = "On";
                f127.Ez_Vehicle_Coverage_Apply_Policy_LevelCoveragesIndicator_A = "On";
            }

            // 2. Policy level coverages (Form 137)
            if (scope.formData && scope.formData.Form137) {
                const f137 = scope.formData.Form137;
                f137.Coverage_CombinedSingleLimit_Amount = "1000000";
                f137.Coverage_Liability_CombinedSingleLimitIndicator = "On";
                f137.Coverage_BodilyInjury_PerPersonLimitAmount = "1000000";
                f137.Coverage_BodilyInjury_PerAccidentLimitAmount = "1000000";
                f137.Coverage_PropertyDamage_PerAccidentLimitAmount = "1000000";
                f137.Coverage_Comprehensive_DeductibleAmount = "1000";
                f137.Coverage_Collision_DeductibleAmount = "1000";
            }

            scope.$apply();
        }""")
        await page.wait_for_timeout(1000)

        # Save & Close
        save_close = page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first
        await save_close.click()
        await page.wait_for_timeout(4000)


if __name__ == "__main__":
    asyncio.run(set_liability_and_comp())
