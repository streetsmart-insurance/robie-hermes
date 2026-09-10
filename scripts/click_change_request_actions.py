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
        
        # In the yellow row (or tr containing 'Change Request'), click Actions dropdown
        actions_btn = page.locator("tr:has-text('Change Request') button:has-text('Actions'), tr:has-text('Change Request') a:has-text('Actions')").first
        if await actions_btn.is_visible():
            logging.info("Clicking Actions on Change Request row...")
            await actions_btn.click()
            await asyncio.sleep(1)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_cr_actions_open.png")
            
            items = await page.evaluate('''() => {
                const els = Array.from(document.querySelectorAll('.dropdown-menu a, .mat-mdc-menu-item, [role=\"menuitem\"]'));
                return els.filter(e => e.offsetParent !== null).map(e => e.innerText.trim());
            }''')
            logging.info(f"Change Request Actions items: {items}")

        # Also let's check Empower Group Geico Auto History!
        # Master ID for Geico: 80767907
        logging.info("Checking Empower Group Geico History tab...")
        await page.goto("https://app.ezlynx.com/applicantportal/policy/80767907/summary/index", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_geico_summary_full.png", full_page=True)
        
        await page.click("a:has-text('History'), button:has-text('History')")
        await asyncio.sleep(2)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/empower_geico_history_full.png", full_page=True)
        
        geico_history_text = await page.evaluate("() => document.body.innerText")
        for line in geico_history_text.split("\n"):
            if any(term in line.lower() for term in ["change", "endorse", "download", "transaction", "effective", "chev", "09/05", "08/21", "08/24"]):
                logging.info(f"  Geico History: {line.strip()}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
