import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.path.append('/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system')
from playwright.async_api import async_playwright
from src.ezlynx.session_manager import EZLynxSessionManager

async def test():
    mgr = EZLynxSessionManager(cdp_url=None)
    async with async_playwright() as p:
        browser, ctx = await mgr.get_authenticated_context(p, headless=True)
        page = await ctx.new_page()
        
        await page.goto("https://app.ezlynx.com/web/account/51287231/policies", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # 1. Check Table View
        table_btn = page.locator("button:has-text('Table view')").first
        if await table_btn.is_visible():
            await table_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_table_view.png")
            logging.info("Saved table view screenshot")

        # 2. Check Service Submenu
        # Re-navigate to policies
        await page.goto("https://app.ezlynx.com/web/account/51287231/policies", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        card = page.locator("mat-card:has-text('CAPI075976')").first
        more_btn = card.locator("button:has-text('more_vert')").first
        await more_btn.click()
        await asyncio.sleep(1)
        
        service_item = page.locator(".mat-mdc-menu-item:has-text('Service')").first
        if await service_item.is_visible():
            await service_item.hover()
            await asyncio.sleep(1)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_service_submenu.png")
            sub_items = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('.mat-mdc-menu-item, [role=\"menuitem\"]')).map(el => el.innerText.trim());
            }''')
            logging.info(f"All Menu items open: {sub_items}")

        # 3. Check what the assignment (clipboard) button does
        assign_btn = card.locator("button:has-text('assignment')").first
        logging.info(f"Assignment button title/aria: {await assign_btn.get_attribute('aria-label')} / {await assign_btn.get_attribute('title')}")
        
        # Click on policy number link or card title
        pol_title = card.locator("text='Auto (Commercial) | CAPI075976'").first
        logging.info("Clicking policy title to see where it leads...")
        await pol_title.click()
        await asyncio.sleep(2)
        logging.info(f"Current URL after clicking title: {page.url}")
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_after_title_click.png")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
