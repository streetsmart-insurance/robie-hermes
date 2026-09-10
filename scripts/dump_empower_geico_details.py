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
        
        await page.goto("https://app.ezlynx.com/applicantportal/policy/80767907/summary/index", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        text = await page.evaluate("() => document.body.innerText")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        
        # Save lines to file for inspection
        with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/empower_geico_summary.txt", "w") as f:
            f.write("\n".join(lines))
            
        logging.info("Wrote empower geico summary text to data/empower_geico_summary.txt")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
