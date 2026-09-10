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
        
        table_btn = page.locator("button:has-text('Table view')").first
        if await table_btn.is_visible():
            await table_btn.click()
            await asyncio.sleep(2)
            
        row_info = await page.evaluate('''() => {
            const rows = Array.from(document.querySelectorAll('tr, mat-row'));
            for (let r of rows) {
                if (r.innerText.includes('CAPI075976')) {
                    const links = Array.from(r.querySelectorAll('a')).map(a => ({href: a.href, text: a.innerText, onclick: a.getAttribute('onclick')}));
                    const buttons = Array.from(r.querySelectorAll('button')).map(b => ({
                        text: b.innerText,
                        aria: b.getAttribute('aria-label'),
                        icon: b.querySelector('mat-icon') ? b.querySelector('mat-icon').innerText : ''
                    }));
                    return { text: r.innerText.replace(/\\n+/g, ' | '), links, buttons };
                }
            }
            return null;
        }''')
        logging.info(f"Row Info: {json.dumps(row_info, indent=2)}")

        # Click the policy number link or row
        pol_elem = page.locator("text='CAPI075976'").first
        await pol_elem.click()
        await asyncio.sleep(3)
        logging.info(f"URL after click: {page.url}")
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/cardone_after_policy_click.png")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
