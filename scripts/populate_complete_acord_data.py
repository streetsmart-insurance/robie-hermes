import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
POLICY_ID = "83280714"
FORM_ID = "480430153"
FORMENTRY_URL = f"https://app.ezlynx.com/applicantportal/Policy/{POLICY_ID}/FormEntry/Index/{FORM_ID}?prevApplied={FORM_ID}"


async def populate_formentry() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print(f"[ACORD SETUP] Navigating directly to FormEntry: {FORMENTRY_URL}...")
        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # 1. VEHICLES SECTION
        print("[ACORD SETUP] Entering Vehicles section...")
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(2000)

        print("[ACORD SETUP] Clicking Edit on Vehicle 1...")
        await page.evaluate("""() => {
            const el = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (el) el.click();
        }""")
        await page.wait_for_timeout(2000)

        modal = page.locator(".repeaterEntryModal.in, div[modal-render='true']").first
        await modal.wait_for(state="visible", timeout=10000)

        print("[ACORD SETUP] Populating complete Vehicle fields...")
        await modal.locator("#Vehicle_VINIdentifier_A").fill("1FTNE2Y84NKA12345")
        await modal.locator("#Vehicle_VINIdentifier_A").dispatch_event("input")
        await modal.locator("#Vehicle_VINIdentifier_A").dispatch_event("change")

        await modal.locator("#Vehicle_ModelYear_A").fill("2022")
        await modal.locator("#Vehicle_ModelYear_A").dispatch_event("input")
        await modal.locator("#Vehicle_ModelYear_A").dispatch_event("change")

        await modal.locator("#Vehicle_ManufacturersName_A").fill("Ford")
        await modal.locator("#Vehicle_ManufacturersName_A").dispatch_event("input")
        await modal.locator("#Vehicle_ManufacturersName_A").dispatch_event("change")

        await modal.locator("#Vehicle_ModelName_A").fill("Transit-250")
        await modal.locator("#Vehicle_ModelName_A").dispatch_event("input")
        await modal.locator("#Vehicle_ModelName_A").dispatch_event("change")

        # Cost New / Value
        cost_inp = modal.locator("#Vehicle_CostNewAmount_A")
        if await cost_inp.count() > 0 and await cost_inp.first.is_visible():
            await cost_inp.first.fill("45000")
            await cost_inp.first.dispatch_event("input")
            await cost_inp.first.dispatch_event("change")

        # Rating Info Accordion
        rating_btn = modal.locator("button.collapsible:has-text('Rating Information')")
        if await rating_btn.count() > 0:
            await rating_btn.first.click()
            await page.wait_for_timeout(1000)

            # Location
            loc_select = modal.locator("select[name='Form127.Ez_Vehicle_RepeaterKey_A'], select[id*='Vehicle_RepeaterKey']")
            if await loc_select.count() > 0 and await loc_select.first.is_visible():
                await loc_select.first.select_option(index=1)
                await loc_select.first.dispatch_event("change")

            use_select = modal.locator("select[name='Vehicle_Use_A'], select[id*='Vehicle_Use']")
            if await use_select.count() > 0 and await use_select.first.is_visible():
                await use_select.first.select_option(index=3)
                await use_select.first.dispatch_event("change")

            radius_select = modal.locator("select[name='Form127.Vehicle_RadiusOfUse_A']")
            if await radius_select.count() > 0 and await radius_select.first.is_visible():
                await radius_select.first.select_option(index=1)
                await radius_select.first.dispatch_event("change")

            rate_class = modal.locator("#Vehicle_RateClassCode_A")
            if await rate_class.count() > 0 and await rate_class.first.is_visible():
                await rate_class.first.fill("01")
                await rate_class.first.dispatch_event("input")
                await rate_class.first.dispatch_event("change")

        # Save Vehicle Modal
        print("[ACORD SETUP] Saving Vehicle Modal...")
        await modal.locator("button.btn-primary:has-text('Save')").first.click()
        await page.wait_for_timeout(3000)

        # 2. DRIVERS SECTION
        print("[ACORD SETUP] Entering Drivers section...")
        await page.locator("a:has-text('Drivers'), span:has-text('Drivers')").first.click()
        await page.wait_for_timeout(2000)

        print("[ACORD SETUP] Clicking Edit on Driver 1...")
        await page.evaluate("""() => {
            const el = document.querySelector("a[data-original-title='Edit'], a[ng-click*='editFormRepeater']");
            if (el) el.click();
        }""")
        await page.wait_for_timeout(2000)

        d_modal = page.locator(".repeaterEntryModal.in, div[modal-render='true']").first
        await d_modal.wait_for(state="visible", timeout=10000)

        print("[ACORD SETUP] Populating complete Driver fields...")
        d_num = d_modal.locator("#Driver_ProducerIdentifier_A")
        if await d_num.count() > 0 and await d_num.first.is_visible():
            await d_num.first.fill("1")
            await d_num.first.dispatch_event("input")
            await d_num.first.dispatch_event("change")

        await d_modal.locator("#Driver_GivenName_A").fill("Carlo")
        await d_modal.locator("#Driver_GivenName_A").dispatch_event("input")
        await d_modal.locator("#Driver_GivenName_A").dispatch_event("change")

        await d_modal.locator("#Driver_Surname_A").fill("Ferrara")
        await d_modal.locator("#Driver_Surname_A").dispatch_event("input")
        await d_modal.locator("#Driver_Surname_A").dispatch_event("change")

        dl_num = d_modal.locator("#Driver_License_LicensePermitNumber_A, input[name*='LicensePermitNumber']")
        if await dl_num.count() > 0 and await dl_num.first.is_visible():
            await dl_num.first.fill("F12345678901234")
            await dl_num.first.dispatch_event("input")
            await dl_num.first.dispatch_event("change")

        dl_state = d_modal.locator("select[id*='License_StateOrProvinceCode'], select[name*='License_StateOrProvinceCode']")
        if await dl_state.count() > 0 and await dl_state.first.is_visible():
            await dl_state.first.select_option(value="NJ")
            await dl_state.first.dispatch_event("change")

        dob = d_modal.locator("#Driver_BirthDt_A, input[name*='BirthDt']")
        if await dob.count() > 0 and await dob.first.is_visible():
            await dob.first.fill("01/01/1985")
            await dob.first.dispatch_event("input")
            await dob.first.dispatch_event("change")

        print("[ACORD SETUP] Saving Driver Modal...")
        await d_modal.locator("button.btn-primary:has-text('Save')").first.click()
        await page.wait_for_timeout(3000)

        # 3. SAVE AND CLOSE
        print("[ACORD SETUP] Saving & Closing FormEntry...")
        save_close = page.locator("#finishButton-header, button:has-text('Save & Close'), a:has-text('Save & Close')").first
        await save_close.click()
        await page.wait_for_timeout(6000)

        print("[ACORD SETUP] Capturing final verification screenshot...")
        await page.screenshot(path="/tmp/robie_live_test/acord_final_verified_live.png")
        print("✓ Screenshot saved to /tmp/robie_live_test/acord_final_verified_live.png")


if __name__ == "__main__":
    asyncio.run(populate_formentry())
