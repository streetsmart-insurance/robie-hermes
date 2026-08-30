import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")


async def inspect_vehicle_errors() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        modal = page.locator(".repeaterEntryModal.in")
        errors = modal.locator(".field-validation-error, .text-danger, .has-error, .ng-invalid-required, [class*='invalid'], [class*='error']")
        e_count = await errors.count()
        print(f"Vehicle modal errors/invalid elements: {e_count}")
        for i in range(e_count):
            err = errors.nth(i)
            etag = await err.evaluate("el => el.tagName")
            eid = await err.get_attribute("id") or ""
            ename = await err.get_attribute("name") or ""
            etext = (await err.text_content() or "").strip()
            print(f"  Err #{i}: tag={etag}, id='{eid}', name='{ename}', text='{etext}'")


if __name__ == "__main__":
    asyncio.run(inspect_vehicle_errors())
