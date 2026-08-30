import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def populate_complete_acord_v2() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print("[ACORD V2] Navigating to FormEntry...")
        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # ----------------------------------------------------
        # 1. DRIVER SETUP
        # ----------------------------------------------------
        print("[ACORD V2] Setting up Driver...")
        await page.locator("a:has-text('Drivers'), span:has-text('Drivers')").first.click()
        await page.wait_for_timeout(1000)

        # Open Driver 1 modal
        await page.evaluate("""() => {
            const editBtn = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (editBtn) editBtn.click();
        }""")
        await page.wait_for_timeout(1500)

        # Set Driver fields in scope & DOM
        await page.evaluate("""() => {
            const modal = document.querySelector('.repeaterEntryModal.in');
            if (!modal) return;
            const scope = angular.element(modal).scope();
            if (scope && scope.Driver && scope.Driver.formData && scope.Driver.formData.Form127) {
                const f = scope.Driver.formData.Form127;
                f.Driver_ProducerIdentifier_A = "1";
                f.Driver_GivenName_A = "Carlo";
                f.Driver_Surname_A = "Ferrara";
                f.Driver_BirthDate_A = "01/01/1985";
                f.Driver_LicenseNumberIdentifier_A = "F12345678901234";
                f.Driver_LicensedStateOrProvinceCode_A = "NJ";
                f.Driver_ExperienceYearCount_A = "15";
                f.Driver_LicensedYear_A = "2005";
                f.Driver_GenderCode_A = "M";
                f.Driver_MaritalStatusCode_A = "M";
                scope.$apply();
            }
        }""")

        await page.locator(".repeaterEntryModal.in #Driver_ProducerIdentifier_A").fill("1")
        await page.locator(".repeaterEntryModal.in #Driver_GivenName_A").fill("Carlo")
        await page.locator(".repeaterEntryModal.in #Driver_Surname_A").fill("Ferrara")
        await page.locator(".repeaterEntryModal.in input[name='Driver_BirthDate_A']").fill("01/01/1985")
        await page.locator(".repeaterEntryModal.in #Driver_LicenseNumberIdentifier_A").fill("F12345678901234")
        if await page.locator(".repeaterEntryModal.in select#Driver_LicensedStateOrProvinceCode_A").count() > 0:
            await page.locator(".repeaterEntryModal.in select#Driver_LicensedStateOrProvinceCode_A").select_option(value="NJ")
        if await page.locator(".repeaterEntryModal.in #Driver_ExperienceYearCount_A").count() > 0:
            await page.locator(".repeaterEntryModal.in #Driver_ExperienceYearCount_A").fill("15")

        # Click Save on Driver modal
        await page.locator(".repeaterEntryModal.in button.btn-primary:has-text('Save')").click()
        await page.wait_for_timeout(2000)

        # ----------------------------------------------------
        # 2. COVERAGES SETUP VIA ANGULAR SCOPE & ACCORDIONS
        # ----------------------------------------------------
        print("[ACORD V2] Setting up Coverages...")
        await page.locator("a:has-text('Coverages'), span:has-text('Coverages')").first.click()
        await page.wait_for_timeout(1000)

        # Expand all collapsibles on Coverages
        await page.evaluate("""() => {
            document.querySelectorAll('button.collapsible, a.collapsible').forEach(b => {
                if (!b.classList.contains('active')) b.click();
            });
        }""")
        await page.wait_for_timeout(1000)

        # Set Form137 Coverages in Angular scope
        await page.evaluate("""() => {
            const el = document.querySelector('[ng-controller], form, .container-fluid');
            if (!el) return;
            const scope = angular.element(el).scope();
            if (scope && scope.formData && scope.formData.Form137) {
                const c = scope.formData.Form137;
                c.Vehicle_BodilyInjury_PerPersonLimitAmount_A = "1000000";
                c.Vehicle_BodilyInjury_PerAccidentLimitAmount_A = "1000000";
                c.Vehicle_PropertyDamage_PerAccidentLimitAmount_A = "1000000";
                c.Vehicle_CombinedSingle_LimitAmount_A = "1000000";
                c.Vehicle_Coverage_ComprehensiveDeductibleIndicator_A = true;
                c.Vehicle_Comprehensive_DeductibleAmount_A = "1000";
                c.Vehicle_Coverage_CollisionIndicator_A = true;
                c.Vehicle_Collision_DeductibleAmount_A = "1000";
                scope.$apply();
            }
        }""")

        # Save Coverages section
        cov_save = page.locator("#save-header, button:has-text('Save'), a:has-text('Save')").first
        if await cov_save.count() > 0:
            await cov_save.click()
            await page.wait_for_timeout(1000)

        # ----------------------------------------------------
        # 3. FINISH & SAVE FORMENTRY
        # ----------------------------------------------------
        print("[ACORD V2] Saving and closing FormEntry...")
        save_close = page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first
        await save_close.click()
        await page.wait_for_timeout(5000)

        print(f"[ACORD V2] Landed on: {page.url}")
        print(f"[ACORD V2] Title: {await page.title()}")

        # ----------------------------------------------------
        # 4. AUTHORITATIVE READ-BACK TABLE DUMP
        # ----------------------------------------------------
        tables = await page.locator("table, .policy-summary-container, .acord-summary, .container-fluid").evaluate_all("""
            els => els.map(e => e.innerText)
        """)
        for idx, t in enumerate(tables):
            if "VEHICLES" in t or "DRIVERS" in t or "COVERAGES" in t:
                print(f"\n================ TABLE #{idx} ================")
                print(t)
                print("==============================================")

        await page.screenshot(path="/tmp/robie_live_test/acord_v2_final_summary.png", full_page=True)
        print("✓ Screenshot saved to /tmp/robie_live_test/acord_v2_final_summary.png")


if __name__ == "__main__":
    asyncio.run(populate_complete_acord_v2())
