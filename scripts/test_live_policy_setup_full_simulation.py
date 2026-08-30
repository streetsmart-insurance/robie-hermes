import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def inspect_status() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].pages[0]

        # Inspect elements matching status
        status_els = page.locator("[id*='Status'], [name*='Status'], [class*='status'], .ui-select-container")
        count = await status_els.count()
        print(f"Elements matching status/select2: {count}")
        for i in range(count):
            el_id = await status_els.nth(i).get_attribute("id")
            el_class = await status_els.nth(i).get_attribute("class")
            print(f"  El #{i}: id={el_id}, class={el_class}")


if __name__ == "__main__":
    asyncio.run(inspect_status())
