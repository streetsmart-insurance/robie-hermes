import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
APPLICANT_ID = "220250093"


async def run_standalone_test() -> None:
    print(f"Connecting to live Chrome session via CDP at {CDP_URL}...")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        contexts = browser.contexts
        if not contexts:
            print("ERROR: No browser contexts found.")
            return

        context = contexts[0]
        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        print(f"Connected to page: {page.url}")

        target_url = f"https://app.ezlynx.com/web/account/{APPLICANT_ID}/policies"
        if page.url != target_url:
            print(f"Navigating to {target_url}...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)

        print(f"Current page title: {await page.title()}")
        print(f"Current page URL: {page.url}")

        # Check for Add Policy button
        add_btn = page.locator("#add-policy, a:has-text('Add Policy'), button:has-text('Add Policy')").first
        has_add = await add_btn.count() > 0
        print(f"Add Policy button found: {has_add}")
        if has_add:
            print(f"Add Policy button text: {await add_btn.inner_text()}")

        # Check for existing policies on table
        table_rows = page.locator("table tbody tr, .policy-card, .k-grid table tbody tr")
        row_count = await table_rows.count()
        print(f"Existing policies/rows detected: {row_count}")

        os.makedirs("/tmp/robie_live_test", exist_ok=True)
        screenshot_path = "/tmp/robie_live_test/policies_success.png"
        await page.screenshot(path=screenshot_path)
        print(f"✓ Screenshot saved to {screenshot_path}")


if __name__ == "__main__":
    asyncio.run(run_standalone_test())
