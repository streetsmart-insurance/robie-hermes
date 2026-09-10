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
        await asyncio.sleep(3)
        
        # Click on the Open Change Request badge
        badge = page.locator("text='Open Change Request effective 8/25/2026'").first
        if await badge.is_visible():
            logging.info("Found badge! Clicking it...")
            await badge.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_change_request_modal.png")
            
            # Extract modal / drawer content
            modal_text = await page.evaluate('''() => {
                const modal = document.querySelector('mat-dialog-container, .cdk-overlay-pane, mat-bottom-sheet-container, .drawer, .modal');
                return modal ? modal.innerText : 'No modal found';
            }''')
            logging.info(f"Modal content: {modal_text}")
        else:
            logging.warning("Badge not found directly.")

        # Also let's inspect the 3-dots menu on CAPI075976 card
        logging.info("Inspecting 3-dots menu on CAPI075976...")
        # Find card container
        card = page.locator("mat-card:has-text('CAPI075976'), .policy-card:has-text('CAPI075976')").first
        if await card.is_visible():
            more_btn = card.locator("button:has-text('more_vert')").first
            await more_btn.click()
            await asyncio.sleep(1.5)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_more_vert_menu.png")
            menu_items = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('.mat-mdc-menu-item, mat-menu-item, [role="menuitem"]')).map(el => el.innerText.trim());
            }''')
            logging.info(f"3-dots Menu items: {menu_items}")

        # Also click dropdown arrow (keyboard_arrow_down) to see policy details view
        arrow_btn = card.locator("button:has-text('keyboard_arrow_down')").first
        if await arrow_btn.is_visible():
            logging.info("Clicking keyboard_arrow_down to expand policy card...")
            await arrow_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_card_expanded.png")
            card_details = await card.inner_text()
            logging.info(f"Expanded Card details: {card_details}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
