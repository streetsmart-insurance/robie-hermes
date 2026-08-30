import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def apply_all_coverages_final() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print("[FINAL SETUP] Navigating to Policies tab...")
        await page.goto("https://app.ezlynx.com/web/account/220250093/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Click top policy-actions button
        print("[FINAL SETUP] Opening Policy actions menu...")
        await page.locator("button#policy-actions").first.click()
        await page.wait_for_timeout(1000)

        # Click a#edit
        print("[FINAL SETUP] Clicking a#edit...")
        await page.locator("a#edit").first.click()
        await page.wait_for_timeout(3000)

        # Click Add & Edit Policy
        print("[FINAL SETUP] Clicking Add & Edit Policy...")
        await page.locator("#AddAndEditPolicyBtn").click()
        await page.wait_for_timeout(4000)
        print(f"[FINAL SETUP] Landed in FormEntry: {page.url}")

        # Go to Vehicles
        print("[FINAL SETUP] Entering Vehicles...")
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(1500)

        # Open Vehicle 1 modal
        await page.evaluate("""() => {
            const editBtn = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (editBtn) editBtn.click();
        }""")
        await page.wait_for_timeout(2000)

        # Set Vehicle Coverages in scope
        print("[FINAL SETUP] Applying Coverages on Vehicle...")
        await page.evaluate("""() => {
            const modal = document.querySelector('.repeaterEntryModal.in');
            if (!modal) return;
            const scope = angular.element(modal).scope();
            if (scope && scope.Vehicle && scope.Vehicle.formData && scope.Vehicle.formData.Form127) {
                const f = scope.Vehicle.formData.Form127;
                f.Ez_Vehicle_Coverage_Apply_Policy_LevelCoveragesIndicator_A = "On";
                f.Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A = "1000";
                f.Vehicle_Collision_DeductibleAmount_A = "1000";
                f.Vehicle_Coverage_LiabilityIndicator_A = "true";
                if (scope.Helpers && scope.Helpers.LobAUTOB && scope.Helpers.LobAUTOB.ApplyPolicyLevelCoverageChanged) {
                    scope.Helpers.LobAUTOB.ApplyPolicyLevelCoverageChanged(scope.Vehicle);
                }
                scope.$apply();
            }
        }""")
        await page.wait_for_timeout(1000)

        # Click Save on Vehicle Modal
        await page.locator(".repeaterEntryModal.in button.btn-primary:has-text('Save')").click()
        await page.wait_for_timeout(2000)

        # Save & Close FormEntry
        print("[FINAL SETUP] Saving & closing FormEntry...")
        save_close = page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first
        await save_close.click()
        await page.wait_for_timeout(4000)
        print(f"[FINAL SETUP] Landed on: {page.url}")

        # Dump tables from Summary
        print("[FINAL SETUP] Reading final summary tables...")
        tables = await page.evaluate("""() => {
            const t = Array.from(document.querySelectorAll('table'));
            return t.map(table => {
                const rows = Array.from(table.querySelectorAll('tr'));
                return rows.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()).join('\\t')).join('\\n');
            });
        }""")

        for i, table_text in enumerate(tables):
            print(f"--- TABLE #{i} ---")
            print(table_text)
            print("--------------------")

        await page.screenshot(path="/tmp/robie_live_test/all_schedules_final_perfect.png", full_page=True)
        print("✓ Screenshot saved to /tmp/robie_live_test/all_schedules_final_perfect.png")


if __name__ == "__main__":
    asyncio.run(apply_all_coverages_final())
