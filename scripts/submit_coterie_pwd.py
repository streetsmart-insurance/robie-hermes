import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from src.security.secrets_manager import secrets_mgr

async def run():
    creds = secrets_mgr.get_login_pair("Coterie")
    password = creds.get("password", "dezrax-myqwy9-wYrrom")
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

        print("Checking Coterie page...")
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(3000)

        # Enter password
        pwd_box = page.locator("input[name='credentials.passcode'], input[type='password'], input#okta-signin-password")
        if await pwd_box.count() > 0:
            print("Entering password...")
            await pwd_box.first.fill(password)
            await page.wait_for_timeout(500)
            verify_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Verify'), button:has-text('Sign In')")
            if await verify_btn.count() > 0:
                print("Clicking Verify...")
                await verify_btn.first.click()
                await page.wait_for_timeout(10000)

        await page.screenshot(path="data/screenshots/coterie_logged_in_landing.png")
        print("Logged in URL:", page.url)

        # Policy Search for CBB-00113127-02
        policy_no = "CBB-00113127-02"
        print(f"Searching for policy {policy_no}...")
        search_box = page.locator("input[placeholder*='Search' i], input[type='search'], input[name*='search' i]")
        if await search_box.count() > 0:
            await search_box.first.fill(policy_no)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(6000)
            await page.screenshot(path="data/screenshots/coterie_policy_search_results.png")
            print("Search screenshot saved: data/screenshots/coterie_policy_search_results.png")

        # Save cookies/storage state
        await page.context.storage_state(path="data/coterie_auth_state.json")
        print("Coterie authentication state saved!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
