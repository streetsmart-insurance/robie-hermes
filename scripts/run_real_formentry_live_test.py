import asyncio
import os
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright
from robie_job_engine.ezlynx_policy_setup import (
    CommercialAutoCoverageItem,
    DriverItem,
    EzlynxPolicySetupPage,
    PolicyShellInput,
    VehicleItem,
)
from robie_job_engine.store import JobStore

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
APPLICANT_ID = "220250093"
POLICY_ID = "83280714"
FORM_ENTRY_URL = f"https://app.ezlynx.com/applicantportal/Policy/{POLICY_ID}/FormEntry/Index/480430153?prevApplied=480430153"
SUMMARY_URL = f"https://app.ezlynx.com/applicantportal/Policy/{POLICY_ID}/summary/index"


async def main() -> None:
    # 1. Mint Job ID
    db_path = os.environ.get("ROBIE_JOBS_DB", "/tmp/robie_test_jobs.db")
    store = JobStore(db_path)
    job = store.create_job(
        action_type="ezlynx_policy_setup",
        payload={
            "applicant_id": APPLICANT_ID,
            "lob": "commercial_auto",
            "policy_number": "CA-ROBIE-LIVE-02",
            "entrypoint": "execute_formentry_schedules_test_only",
            "commit": "930fd09",
            "pr": "108",
        },
    )
    job_id = job["id"]
    print(f"[LIVE RUN] Minted ROBIE Job ID: {job_id}")

    shell_input = PolicyShellInput(
        applicant_id=APPLICANT_ID,
        lob="commercial_auto",
        policy_number="CA-ROBIE-LIVE-02",
        vehicles=(
            VehicleItem(
                vin="1FTNE2Y84NKA12345",
                year="2022",
                make="Ford",
                model="Transit-250",
                cost_new="45000",
                vehicle_type="Commercial",
                radius="Local 0-50 Miles",
                use="Comm'l",
                rate_class="01",
                premium="2450.00",
                comp_deductible="1000",
                coll_deductible="1000",
            ),
        ),
        drivers=(
            DriverItem(
                first_name="Carlo",
                last_name="Ferrara",
                dob="01/01/1985",
                license_number="F12345678901234",
                license_state="NJ",
                experience_years="15",
                licensed_year="2005",
            ),
        ),
        commercial_auto_coverage=CommercialAutoCoverageItem(
            csl_limit="1000000",
            bi_per_person="1000000",
            bi_per_accident="1000000",
            pd_per_accident="1000000",
            um_uim_limit="1000000",
            med_pay="5000",
            pip="250000",
        ),
    )

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        print(f"[LIVE RUN] Navigating to FormEntry: {FORM_ENTRY_URL}")
        await page.goto(FORM_ENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        page_obj = EzlynxPolicySetupPage(page)

        print("[LIVE RUN] Calling execute_formentry_schedules_test_only()...")
        res = await page_obj.execute_formentry_schedules_test_only(shell_input)
        print(f"[LIVE RUN] Schedules applied: {res['schedules_applied']}")

        print("[LIVE RUN] Saving and closing FormEntry...")
        await page_obj.save_and_close_form_entry()
        await page.wait_for_timeout(3000)

        print(f"[LIVE RUN] Navigating to Summary view: {SUMMARY_URL}")
        await page.goto(SUMMARY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        screenshot_path = "/tmp/robie_live_test/coverage_grid_pr108_verified.png"
        Path("/tmp/robie_live_test").mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"[LIVE RUN] Screenshot saved to: {screenshot_path}")


if __name__ == "__main__":
    asyncio.run(main())
