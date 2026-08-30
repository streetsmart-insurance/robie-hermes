import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def apply_policy_coverages() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print("[COVERAGES] Opening Policies tab...")
        await page.goto("https://app.ezlynx.com/web/account/220250093/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Open actions menu on CA-ROBIE-LIVE-02
        await page.locator("button#policy-actions, button.mat-mdc-menu-trigger, button:has-text('more_vert')").first.click()
        await page.wait_for_timeout(1000)

        # Click Edit
        await page.locator(".cdk-overlay-container button:has-text('Edit'), .mat-mdc-menu-content button:has-text('Edit')").first.click()
        await page.wait_for_timeout(2000)

        # Click Add & Edit Policy
        await page.locator("#AddAndEditPolicyBtn").click()
        await page.wait_for_timeout(4000)

        print(f"[COVERAGES] Entered FormEntry: {page.url}")

        # Go to Vehicles
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(1000)

        # Open Vehicle 1 modal
        await page.evaluate("""() => {
            const editBtn = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (editBtn) editBtn.click();
        }""")
        await page.wait_for_timeout(1500)

        # Set Vehicle Coverages in scope
        await page.evaluate("""() => {
            const modal = document.querySelector('.repeaterEntryModal.in');
            if (!modal) return;
            const scope = angular.element(modal).scope();
            if (scope && scope.Vehicle && scope.Vehicle.formData && scope.Vehicle.formData.Form127) {
                scope.Vehicle.formData.Form127.Ez_Vehicle_Coverage_Apply_Policy_LevelCoveragesIndicator_A = "On";
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
        save_close = page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first
        await save_close.click()
        await page.wait_for_timeout(4000)

        print(f"[COVERAGES] Landed on: {page.url}")


if __name__ == "__main__":
    asyncio.run(apply_policy_coverages())
