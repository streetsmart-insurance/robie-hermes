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
        
        await page.click("a:has-text('History'), button:has-text('History')")
        await asyncio.sleep(2)
        
        # In history, let's look at the rows and check if we can see transaction details
        # Let's inspect all transaction links/buttons
        rows_data = await page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('tr'));
            return rows.map(r => ({
                text: r.innerText.replace(/\\s+/g, ' ').trim(),
                buttons: Array.from(r.querySelectorAll('button, a')).map(b => b.innerText.trim())
            }));
        }''')
        
        print("--- HISTORY ROWS ---")
        for r in rows_data:
            if any(k in r['text'] for k in ['Change Request', 'Policy Change', 'Renewal']):
                print(r)
                
        # Click Actions on Change Request -> Compare
        cr_actions = page.locator("tr:has-text('Change Request') button:has-text('Actions'), tr:has-text('Change Request') a:has-text('Actions')").first
        await cr_actions.click()
        await asyncio.sleep(1)
        compare_btn = page.locator(".dropdown-menu a:has-text('Compare'), [role=\"menuitem\"]:has-text('Compare'), a:has-text('Compare')").first
        if await compare_btn.is_visible():
            await compare_btn.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_compare_page.png", full_page=True)
            comp_text = await page.evaluate("() => document.body.innerText")
            print("--- COMPARE PAGE CONTENT ---")
            for line in comp_text.split("\n"):
                if any(k in line.lower() for k in ["diff", "change", "driver", "rutledge", "cardone", "added", "removed", "modified"]):
                    print("  COMPARE:", line.strip())

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
