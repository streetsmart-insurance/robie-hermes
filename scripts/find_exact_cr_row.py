import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("find_cr")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.set_viewport_size({"width": 1600, "height": 1000})

        h_url = "https://app.ezlynx.com/applicantportal/policy/75473629/history/index"
        logger.info(f"Navigating to {h_url}...")
        await page.goto(h_url, wait_until="networkidle", timeout=45000)
        await asyncio.sleep(3)

        # Look for row containing EditChangeRequest
        edit_link = page.locator("a[href*='EditChangeRequest']").first
        count = await edit_link.count()
        logger.info(f"Found EditChangeRequest count: {count}")
        if count > 0:
            parent_tr = edit_link.locator("xpath=ancestor::tr[1]")
            logger.info(f"Row inner text: {await parent_tr.inner_text()}")
            logger.info(f"Row HTML: {await parent_tr.evaluate('e => e.innerHTML')}")

            # Inside this row or its Actions menu, look for Delete
            # Usually in EZLynx, clicking Actions opens a dropdown
            actions_in_row = parent_tr.locator("text='Actions', [title*='action'], .dropdown-toggle").first
            logger.info(f"Found actions in row: {await actions_in_row.count()}")
            await actions_in_row.click()
            await asyncio.sleep(1)

            # Look for Delete button inside this row
            delete_in_row = parent_tr.locator("a:has-text('Delete'), button:has-text('Delete')").first
            logger.info(f"Found delete in row: {await delete_in_row.count()}, visible: {await delete_in_row.is_visible()}")
            if await delete_in_row.count() > 0:
                logger.info(f"Delete HTML: {await delete_in_row.evaluate('e => e.outerHTML')}")
        
        await page.screenshot(path="data/screenshots/cr_row_actions.png")
        await page.close()

if __name__ == "__main__":
    asyncio.run(main())
