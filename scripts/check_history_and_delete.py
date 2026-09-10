import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("check_history")

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
        
        await page.screenshot(path="data/screenshots/lawn_buddies_history.png")
        logger.info("Saved lawn_buddies_history.png")
        
        # Look for table rows, buttons, actions in history
        rows = await page.locator("tr").all()
        logger.info(f"Found {len(rows)} table rows in history.")
        for idx, r in enumerate(rows):
            txt = (await r.inner_text()).strip().replace("\n", " | ")
            logger.info(f"Row {idx}: {txt}")
            
        # Look for buttons or links in rows
        action_elements = await page.locator("a, button, [title], [class*='action'], [class*='menu']").all()
        for el in action_elements:
            t = (await el.inner_text()).strip()
            title = await el.get_attribute("title") or ""
            href = await el.get_attribute("href") or ""
            if any(k in f"{t} {title} {href}".lower() for k in ["delete", "cancel", "void", "remove", "action", "change"]):
                logger.info(f"Matching element: text='{t}', title='{title}', href='{href}'")

        await page.close()

if __name__ == "__main__":
    asyncio.run(main())
