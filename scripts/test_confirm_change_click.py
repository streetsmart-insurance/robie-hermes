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
        
        await page.goto("https://app.ezlynx.com/applicantportal/policy/31887060/summary/index", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Click History tab
        await page.click("a:has-text('History'), button:has-text('History')")
        await asyncio.sleep(2)
        
        # Click Actions on Change Request row
        actions_btn = page.locator("tr:has-text('Change Request') button:has-text('Actions'), tr:has-text('Change Request') a:has-text('Actions')").first
        await actions_btn.click()
        await asyncio.sleep(1)
        
        confirm_btn = page.locator(".dropdown-menu a:has-text('Confirm Change'), [role=\"menuitem\"]:has-text('Confirm Change'), a:has-text('Confirm Change')").first
        logging.info("Clicking Confirm Change...")
        await confirm_btn.click()
        await asyncio.sleep(2)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_confirm_change_modal.png")
        modal_text = await page.evaluate('''() => {
            const m = document.querySelector('.modal-dialog, .modal-content, mat-dialog-container, .cdk-overlay-pane, form');
            return m ? m.innerText : 'No modal container';
        }''')
        logging.info(f"Modal Text: {modal_text}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
