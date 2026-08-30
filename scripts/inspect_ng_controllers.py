import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
FORMENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/83280714/FormEntry/Index/480430153?prevApplied=480430153"


async def inspect_ng_controllers() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        info = await page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('[ng-controller], .ng-scope'));
            return els.map(e => ({
                tag: e.tagName,
                id: e.id,
                controller: e.getAttribute('ng-controller'),
                scopeKeys: Object.keys(angular.element(e).scope() || {})
            })).slice(0, 10);
        }""")
        print("CONTROLLERS:")
        for item in info:
            print(item)


if __name__ == "__main__":
    asyncio.run(inspect_ng_controllers())
