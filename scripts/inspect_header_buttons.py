import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def inspect_header_buttons() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print(f"Current URL: {page.url}")
        buttons = await page.locator("button, a.btn, input[type='button'], input[type='submit']").evaluate_all("""
            els => els.map(e => ({
                id: e.id,
                text: e.innerText.trim(),
                value: e.value,
                className: e.className,
                visible: e.offsetParent !== null
            })).filter(b => b.text || b.value || b.id)
        """)
        print(f"Buttons found ({len(buttons)}):")
        for b in buttons:
            if "save" in b['text'].lower() or "close" in b['text'].lower() or "finish" in b['id'].lower() or "save" in str(b['id']).lower():
                print(f"  {b}")


if __name__ == "__main__":
    asyncio.run(inspect_header_buttons())
