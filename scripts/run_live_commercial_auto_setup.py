import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
APPLICANT_ID = "220250093"
POLICY_NUM = "CA-ROBIE-LIVE-02"


async def run_production_setup() -> None:
    print(f"[LIVE PRODUCTION RUN] Starting Commercial Auto policy setup for Policy #{POLICY_NUM}...")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # Step 1: Navigate to Policies tab
        print(f"[LIVE STEP 1] Navigating to Policies tab for Applicant {APPLICANT_ID}...")
        await page.goto(f"https://app.ezlynx.com/web/account/{APPLICANT_ID}/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Step 2: Click '+ Add policy'
        print("[LIVE STEP 2] Opening Policy Add modal...")
        add_btn = page.locator("#add-policy, a:has-text('Add policy'), button:has-text('Add policy')")
        await add_btn.first.wait_for(state="visible", timeout=10000)
        await add_btn.first.click()
        await page.wait_for_timeout(4000)

        # Step 3: Fill Policy Shell
        print("[LIVE STEP 3] Filling Policy Shell details...")
        # 3a. Line of Business
        lob = page.locator("#mergeSplitLOB")
        await lob.wait_for(state="visible", timeout=10000)
        await lob.select_option(label="Auto (Commercial)")
        await lob.dispatch_event("change")
        await page.wait_for_timeout(500)

        # 3b. Department (Select2 multi-select)
        dept_match = page.locator("#Department [aria-label='Select box select'], #Department .select2-choice")
        if await dept_match.count() > 0:
            await dept_match.first.click()
            await page.wait_for_timeout(500)
            comm_choice = page.locator(".ui-select-choices-row, .select2-result-selectable").filter(has_text="Commercial Lines (CL)")
            if await comm_choice.count() > 0:
                await comm_choice.first.click()
                await page.wait_for_timeout(500)

        # 3c. Policy Number
        pol_num_input = page.locator("#PolicyNumber")
        await pol_num_input.fill(POLICY_NUM)
        await pol_num_input.dispatch_event("input")
        await pol_num_input.dispatch_event("change")

        # 3d. Transaction Type
        trans = page.locator("#TransactionType")
        await trans.select_option(label="New Business")
        await trans.dispatch_event("change")
        await page.wait_for_timeout(500)

        # 3e. Master Company
        master = page.locator("#MasterCompany")
        await master.select_option(label="Progressive Insurance")
        await master.dispatch_event("change")
        await page.wait_for_timeout(1000)

        # 3f. Writing Company
        writing = page.locator("#WritingCompany")
        if await writing.count() > 0 and await writing.is_visible():
            options = await writing.locator("option").all_inner_texts()
            prog_opts = [o.strip() for o in options if "PROGRESSIVE" in o.upper()]
            if prog_opts:
                await writing.select_option(label=prog_opts[0])
                await writing.dispatch_event("change")
                await page.wait_for_timeout(500)

        # 3g. Billing Type & Rating State
        billing = page.locator("#BillingType")
        if await billing.count() > 0:
            await billing.select_option(label="Direct")
            await billing.dispatch_event("change")
            await page.wait_for_timeout(500)

        state_select = page.locator("#RatingState")
        if await state_select.count() > 0:
            await state_select.select_option(label="NJ")
            await state_select.dispatch_event("change")
            await page.wait_for_timeout(500)

        # 3h. Dates
        eff = page.locator("#EffectiveDate")
        await eff.fill("09/01/2026")
        await eff.dispatch_event("input")
        await eff.dispatch_event("change")

        exp = page.locator("#ExpirationDate")
        await exp.fill("09/01/2027")
        await exp.dispatch_event("input")
        await exp.dispatch_event("change")

        orig = page.locator("#LOBOriginationDate")
        if await orig.count() > 0:
            await orig.fill("09/01/2026")
            await orig.dispatch_event("input")
            await orig.dispatch_event("change")

        # 3i. Commission & Premiums
        comm = page.locator("#TotalCommission")
        if await comm.count() > 0:
            await comm.fill("12.00")
            await comm.dispatch_event("input")
            await comm.dispatch_event("change")

        for fid in ["#Premium", "#FullTermPremium", "#AnnualPremium"]:
            el = page.locator(fid)
            if await el.count() > 0:
                await el.fill("2450.00")
                await el.dispatch_event("input")
                await el.dispatch_event("change")

        print("[LIVE STEP 3 COMPLETE] Policy Shell fully populated.")

        # Step 4: Submit Policy
        print("[LIVE STEP 4] Submitting policy creation form...")
        submit_btn = page.locator("#AddPolicyBtn, button:has-text('Add Policy'), input[value='Add Policy']")
        await submit_btn.first.scroll_into_view_if_needed()
        await submit_btn.first.click()
        await page.wait_for_timeout(8000)

        # Step 5: Read-back & Verify on Policies Tab
        print("[LIVE STEP 5] Authoritative read-back from Policies tab...")
        await page.goto(f"https://app.ezlynx.com/web/account/{APPLICANT_ID}/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        body_text = await page.locator("body").inner_text()
        has_new_policy = POLICY_NUM in body_text
        print(f"Policy {POLICY_NUM} present on server: {has_new_policy}")

        # Capture evidence screenshot
        screenshot_path = f"/tmp/robie_live_test/live_run_{POLICY_NUM.lower().replace('-', '_')}.png"
        await page.screenshot(path=screenshot_path)
        print(f"✓ Screenshot saved to {screenshot_path}")

        # Step 6: Add Discussion Note
        print("[LIVE STEP 6] Logging verified Discussion Note...")
        add_note_btn = page.locator("#add-note-header, [data-testid='add-note-header']")
        if await add_note_btn.count() > 0:
            await add_note_btn.first.click()
            await page.wait_for_timeout(1000)

            title_input = page.locator("#txtDiscussionTitle, [name='discussionTitle']")
            if await title_input.count() > 0:
                await title_input.first.fill(f"New Policy Setup - {POLICY_NUM}")

            body_input = page.locator("#txtDiscussionBody, textarea.k-editor-textarea, [name='discussionBody'], .note-editor textarea, [contenteditable='true']")
            if await body_input.count() > 0:
                await body_input.first.fill(
                    f"Completed Commercial Auto policy setup for Policy #{POLICY_NUM} with Progressive Commercial.\n"
                    f"Premium: $2,450.00 | Term: 09/01/2026 - 09/01/2027 | State: NJ\n\n"
                    f"ROBIE was here"
                )

            save_note = page.locator("#btnSaveNote, button:has-text('Save')")
            if await save_note.count() > 0:
                await save_note.first.click()
                await page.wait_for_timeout(2000)
                print("Discussion note logged successfully.")


if __name__ == "__main__":
    asyncio.run(run_production_setup())
