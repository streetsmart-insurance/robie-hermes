import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def fix_all_fields_live() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        # Navigate to FormEntry for CA-ROBIE-LIVE-02
        form_entry_url = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"
        await page.goto(form_entry_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        
        # Populate Coverages via Angular scope
        res = await page.evaluate("""() => {
            const scope = angular.element(document.querySelector('#acordFormEntry') || document.body).scope();
            if (!scope || !scope.formData) return {error: "no scope"};
            
            // 1. Form 137 (Commercial Auto Coverages)
            if (!scope.formData.Form137) scope.formData.Form137 = {};
            scope.formData.Form137.Coverage_CombinedSingleLimit_Indicator = "On";
            scope.formData.Form137.Coverage_CombinedSingleLimit_PerAccidentLimitAmount = "1000000";
            scope.formData.Form137.Vehicle_BodilyInjury_PerPersonLimitAmount_A = "1000000";
            scope.formData.Form137.Vehicle_BodilyInjury_PerAccidentLimitAmount_A = "1000000";
            scope.formData.Form137.Vehicle_PropertyDamage_PerAccidentLimitAmount_A = "1000000";
            scope.formData.Form137.Vehicle_UninsuredUnderinsuredMotorists_BodilyInjuryPerAccidentLimitAmount_A = "1000000";
            scope.formData.Form137.Vehicle_MedicalPayments_PerPersonLimitAmount_A = "5000";
            scope.formData.Form137.Vehicle_PersonalInjuryProtection_PerPersonLimitAmount_A = "250000";
            
            // 2. Form 127 (Vehicle 1 Details & Coverages)
            if (scope.formData.Form127) {
                scope.formData.Form127.Vehicle_VehicleType_A = "Commercial";
                scope.formData.Form127.Vehicle_Coverage_LiabilityIndicator_A = "On";
                scope.formData.Form127.Ez_Vehicle_Coverage_Apply_Policy_LevelCoveragesIndicator_A = "On";
                scope.formData.Form127.Vehicle_Coverage_ComprehensiveDeductibleIndicator_A = "On";
                scope.formData.Form127.Vehicle_Comprehensive_DeductibleAmount_A = "1000";
                scope.formData.Form127.Vehicle_Coverage_CollisionIndicator_A = "On";
                scope.formData.Form127.Vehicle_Collision_DeductibleAmount_A = "1000";
            }
            
            scope.$apply();
            
            // Trigger save
            if (typeof scope.saveAcordFormData === "function") {
                scope.saveAcordFormData();
                return {saved: true};
            }
            return {saved: false};
        }""")
        print("Scope save result:", res)
        await page.wait_for_timeout(3000)
        
        # Navigate back to Summary and inspect tables
        summary_url = "https://app.ezlynx.com/applicantportal/Policy/83280714/summary/index?pollForUpdatedAcord=true"
        await page.goto(summary_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.screenshot(path="/tmp/robie_live_test/fixed_summary_proof.png", full_page=True)
        print("Saved proof screenshot.")


if __name__ == "__main__":
    asyncio.run(fix_all_fields_live())
