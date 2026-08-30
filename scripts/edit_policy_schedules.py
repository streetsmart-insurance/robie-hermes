import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def inspect_formentry_sections() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print("Inspecting FormEntry navigation items...")
        nav_items = page.locator("a, button, span.k-link, [role='treeitem'], .form-entry-nav a, .sections a, ul.k-group a")
        count = await nav_items.count()
        print(f"Total nav items: {count}")
        for i in range(count):
            item = nav_items.nth(i)
            t = (await item.inner_text()).strip()
            href = await item.get_attribute("href") or ""
            if any(k in t.lower() for k in ["vehicle", "driver", "location", "coverage", "finish", "save"]):
                print(f"  Nav #{i}: text='{t}', href='{href}'")

        await page.screenshot(path="/tmp/robie_live_test/formentry_nav_inspected.png")


if __name__ == "__main__":
    asyncio.run(inspect_formentry_sections())
