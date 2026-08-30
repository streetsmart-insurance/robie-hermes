import asyncio
import os
import sys

from playwright.async_api import async_playwright

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
POLICY_ID = "83280714"
FORM_ID = "480430153"
FORMENTRY_URL = f"https://app.ezlynx.com/applicantportal/Policy/{POLICY_ID}/FormEntry/Index/{FORM_ID}"


async def inspect_and_fix_formentry() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        print(f"[FIX DATA] Navigating to FormEntry: {FORMENTRY_URL}...")
        await page.goto(FORMENTRY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # 1. Inspect Vehicles Table
        print("[FIX DATA] Inspecting Vehicles section...")
        await page.locator("a:has-text('Vehicles'), span:has-text('Vehicles')").first.click()
        await page.wait_for_timeout(2000)

        # Find existing vehicle row and click Edit
        edit_veh_btns = page.locator("button:has-text('Edit'), a:has-text('Edit'), [title='Edit']")
        print(f"Vehicle Edit buttons found: {await edit_veh_btns.count()}")
        if await edit_veh_btns.count() > 0:
            await edit_veh_btns.first.click()
            await page.wait_for_timeout(2000)
            
            # Inspect vehicle modal inputs
            modal = page.locator(".repeaterEntryModal.in, div[modal-render='true']")
            inputs = await modal.locator("input, select").evaluate_all("""
                els => els.map(e => ({
                    tag: e.tagName,
                    id: e.id,
                    name: e.name,
                    val: e.value,
                    model: e.getAttribute('ng-model')
                }))
            """)
            print(f"Vehicle modal inputs count: {len(inputs)}")
            for idx, inp in enumerate(inputs):
                if inp.get('id') or inp.get('name') or inp.get('model'):
                    print(f"  Veh Inp #{idx}: id={inp.get('id')}, name={inp.get('name')}, val={inp.get('val')}, model={inp.get('model')}")

        await page.screenshot(path="/tmp/robie_live_test/fix_vehicle_inspect.png")
        print("✓ Screenshot saved to /tmp/robie_live_test/fix_vehicle_inspect.png")


if __name__ == "__main__":
    asyncio.run(inspect_and_fix_formentry())
