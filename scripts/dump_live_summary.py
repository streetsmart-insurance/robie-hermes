import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
SUMMARY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/summary/index"


async def dump_live_summary() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(SUMMARY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        tables = await page.evaluate("""() => {
            const t = Array.from(document.querySelectorAll('table'));
            return t.map(table => {
                const rows = Array.from(table.querySelectorAll('tr'));
                return rows.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()).join('\\t')).join('\\n');
            });
        }""")

        for i, table_text in enumerate(tables):
            print(f"=== TABLE #{i} ===")
            print(table_text)
            print("===================")

        await page.screenshot(path="/tmp/robie_live_test/current_policy_summary.png", full_page=True)


if __name__ == "__main__":
    asyncio.run(dump_live_summary())
