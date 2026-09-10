import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delete_cr")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.set_viewport_size({"width": 1600, "height": 1000})

        h_url = "https://app.ezlynx.com/applicantportal/policy/75473629/history/index"
        logger.info(f"Navigating to {h_url}...")
        await page.goto(h_url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(4)

        # Handle dialog if any
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

        # Locate the Delete link
        delete_link = page.locator("a:has-text('Delete')").first
        if await delete_link.count() > 0:
            logger.info("Delete link found:")
            html = await delete_link.evaluate("e => e.outerHTML")
            logger.info(f"Outer HTML: {html}")

            # Click Actions dropdown first if hidden
            actions_btn = page.locator(".dropdown-toggle:has-text('Actions'), a:has-text('Actions')").first
            try:
                await actions_btn.click()
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Actions click: {e}")

            # Click Delete
            logger.info("Clicking Delete...")
            await delete_link.click()
            await asyncio.sleep(3)

            # Check if modal or confirmation appears
            confirm_btn = page.locator("button:has-text('Yes'), button:has-text('Delete'), button:has-text('Confirm'), .modal-footer button.btn-primary").first
            if await confirm_btn.count() > 0 and await confirm_btn.is_visible():
                logger.info(f"Confirm button visible: {await confirm_btn.inner_text()}")
                await confirm_btn.click()
                await asyncio.sleep(3)

            await page.screenshot(path="data/screenshots/after_delete_cr.png")
            logger.info("Saved after_delete_cr.png")
        else:
            logger.error("Delete link NOT found!")

        await page.close()

if __name__ == "__main__":
    asyncio.run(main())
