import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def run():
    profile_dir = Path.home() / ".coterie_chrome_profile"
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            args=["--no-sandbox"]
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.goto("https://dashboard.coterieinsurance.com/", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Fill username
        user_sel = page.locator("input[name='identifier'], input[name='username']")
        if await user_sel.count() > 0:
            await user_sel.first.fill("carlo@streetsmart.insurance")
            await page.locator("input[type='submit'], button[type='submit']").first.click()
            await page.wait_for_timeout(3000)

        # Dump buttons / links text and tags
        elements = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('a, button, input[type="submit"], input[type="button"]')).map(el => ({
                tag: el.tagName,
                text: el.innerText || el.value,
                className: el.className,
                href: el.href || null
            }));
        }''')
        print("Clickable elements on page:")
        for el in elements:
            print(el)
        await browser.close()

asyncio.run(run())
