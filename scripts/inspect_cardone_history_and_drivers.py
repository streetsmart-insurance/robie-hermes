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
        
        # 1. Full page summary screenshot to see drivers
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_summary_full.png", full_page=True)
        
        # Extract all text on summary page
        summary_text = await page.evaluate("() => document.body.innerText")
        logging.info("Checking drivers on summary page...")
        for line in summary_text.split("\n"):
            if any(term in line.lower() for term in ["driver", "rutledge", "alexander", "kevin", "vehicle"]):
                logging.info(f"  Summary line: {line.strip()}")

        # 2. Click the History tab!
        history_tab = page.locator("a:has-text('History'), button:has-text('History')").first
        if await history_tab.is_visible():
            logging.info("Clicking History tab...")
            await history_tab.click()
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_history_full.png", full_page=True)
            history_text = await page.evaluate("() => document.body.innerText")
            logging.info("History page content snippet:")
            for line in history_text.split("\n"):
                if any(term in line.lower() for term in ["change", "endorse", "download", "transaction", "effective", "driver", "09/05", "08/25"]):
                    logging.info(f"  History line: {line.strip()}")

        # 3. Check Policy Actions dropdown (does it have 'Confirm Change Request' or similar?)
        actions_btn = page.locator("button:has-text('Actions')").first
        if await actions_btn.is_visible():
            await actions_btn.click()
            await asyncio.sleep(1)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_policy_actions.png")
            actions_menu = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('.mat-mdc-menu-item, [role=\"menuitem\"], .dropdown-menu a')).map(el => el.innerText.trim());
            }''')
            logging.info(f"Policy Actions Menu: {actions_menu}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
