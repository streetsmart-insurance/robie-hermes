import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def simulate_phase5() -> None:
    print("[SIMULATION Phase 5] Inspecting Billing Company & Writing Company...")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].pages[0]

        # 1. Writing Company
        writing_sel = page.locator("#WritingCompany")
        if await writing_sel.count() > 0:
            writing_opts = await writing_sel.locator("option").all_inner_texts()
            print(f"  Writing Company options: {writing_opts}")
            prog_w = [opt for opt in writing_opts if "progressive" in opt.lower() or opt.strip() != "---Select---"]
            if len(prog_w) > 1:
                # pick first non-empty option
                pick = [o for o in prog_w if "---" not in o][0]
                print(f"  Selecting Writing Company: {pick}")
                await writing_sel.select_option(label=pick)
                await writing_sel.dispatch_event("change")
                await page.wait_for_timeout(500)

        # 2. Billing Company
        billing_sel = page.locator("#BillingCompany")
        if await billing_sel.count() > 0:
            billing_opts = await billing_sel.locator("option").all_inner_texts()
            print(f"  Billing Company options: {billing_opts}")
            prog_b = [opt for opt in billing_opts if "progressive" in opt.lower() or opt.strip() != "---Select---"]
            if len(prog_b) > 1:
                pick = [o for o in prog_b if "---" not in o][0]
                print(f"  Selecting Billing Company: {pick}")
                await billing_sel.select_option(label=pick)
                await billing_sel.dispatch_event("change")
                await page.wait_for_timeout(500)

        await page.screenshot(path="/tmp/robie_live_test/step5_billing_writing_filled.png")

        # 3. Click Add & Edit Policy
        print("[SIMULATION] Clicking '#AddAndEditPolicyBtn'...")
        add_edit_btn = page.locator("#AddAndEditPolicyBtn").first
        await add_edit_btn.click()
        await page.wait_for_timeout(5000)

        print(f"  URL after Add & Edit: {page.url}")
        print(f"  Title: {await page.title()}")
        await page.screenshot(path="/tmp/robie_live_test/step6_advanced_to_policy_details.png")
        print("  Step 6 screenshot saved.")


if __name__ == "__main__":
    asyncio.run(simulate_phase5())
