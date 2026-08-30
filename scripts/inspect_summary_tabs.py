import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def dump_summary_tables() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        tables = await page.locator("table, .policy-summary-container, .acord-summary, .container-fluid").evaluate_all("""
            els => els.map(e => e.innerText)
        """)
        for idx, t in enumerate(tables):
            if "VEHICLES" in t or "DRIVERS" in t or "Transit" in t or "Carlo" in t:
                print(f"--- TABLE #{idx} ---")
                print(t)
                print("--------------------")

        await page.screenshot(path="/tmp/robie_live_test/full_summary_page.png", full_page=True)
        print("✓ Full page screenshot saved to /tmp/robie_live_test/full_summary_page.png")


if __name__ == "__main__":
    asyncio.run(dump_summary_tables())
