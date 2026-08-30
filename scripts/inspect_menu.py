import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def inspect_menu_html() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto("https://app.ezlynx.com/web/account/220250093/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Click policy-actions
        await page.locator("button#policy-actions").first.click()
        await page.wait_for_timeout(1000)

        # Inspect all overlay elements
        html = await page.evaluate("""() => {
            const overlays = Array.from(document.querySelectorAll('.cdk-overlay-container, .mat-mdc-menu-panel, [role="menu"]'));
            return overlays.map(o => o.outerHTML).join('\\n---\\n');
        }""")
        print("OVERLAYS:")
        print(html)


if __name__ == "__main__":
    asyncio.run(inspect_menu_html())
