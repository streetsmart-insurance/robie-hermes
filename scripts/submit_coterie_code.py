import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def run():
    code = "446968"
    profile_dir = Path.home() / ".coterie_chrome_profile"
    downloads_dir = Path("data/downloads")
    downloads_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()

        print("Navigating to Coterie login...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Check if we need to click Enter code instead
        code_link = page.get_by_text("Enter a verification code instead")
        if await code_link.count() > 0:
            print("Clicking Enter a verification code instead...")
            await code_link.first.click()
            await page.wait_for_timeout(2000)

        # Enter the 6-digit code
        print(f"Entering verification code: {code}...")
        code_input = page.locator("input[name='credentials.passcode'], input[placeholder*='code' i], input[type='text'], input#okta-signin-passcode")
        if await code_input.count() > 0:
            await code_input.first.fill(code)
            await page.wait_for_timeout(500)
            
            verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify')")
            if await verify_btn.count() > 0:
                print("Clicking Verify...")
                await verify_btn.first.click()
                await page.wait_for_timeout(8000)

        await page.screenshot(path="data/screenshots/coterie_logged_in_dashboard.png")
        print("Logged in URL:", page.url)

        # Now search for policy CBB-00113127-02
        policy_no = "CBB-00113127-02"
        print(f"Searching for policy {policy_no}...")
        search_box = page.locator("input[placeholder*='Search' i], input[type='search'], input[name*='search' i]")
        if await search_box.count() > 0:
            await search_box.first.fill(policy_no)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
            await page.screenshot(path="data/screenshots/coterie_policy_search_results.png")
            print("Search screenshot saved: data/screenshots/coterie_policy_search_results.png")

        # Save all cookies/storage state
        await page.context.storage_state(path="data/coterie_auth_state.json")
        print("Coterie authentication state saved!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
