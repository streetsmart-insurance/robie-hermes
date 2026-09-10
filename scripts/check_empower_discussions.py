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
        
        await page.goto("https://app.ezlynx.com/web/account/69070440/activity", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Fetch discussions
        disc_res = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=20&applicantId=69070440&applicantContext=true');
            return r.ok ? await r.json() : null;
        }''')
        
        if disc_res and "discussions" in disc_res:
            logging.info(f"Empower Discussions count: {len(disc_res['discussions'])}")
            for d in disc_res['discussions']:
                title = d.get('title')
                d_id = d.get('discussionId') or d.get('id')
                status = d.get('status')
                logging.info(f"  Discussion: ID={d_id} | Title='{title}' | Status={status} | Date={d.get('dateCreated')}")
                if "9300334879" in title or "auto" in title.lower() or "chev" in title.lower():
                    logging.info(f"    Full Auto Discussion: {json.dumps(d, indent=2)}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
