import asyncio
import os
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("get_solano_idcard")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        
        # Find the active NatGen page
        page = None
        for pg in ctx.pages:
            if "natgenagency.com" in pg.url:
                page = pg
                break
        if not page:
            page = await ctx.new_page()
            await page.goto("https://natgenagency.com/Policy/PolicySummary.aspx?PolicyNumber=2031936859")
        else:
            logger.info(f"Using existing page: {page.url}")
            await page.goto("https://natgenagency.com/Policy/PolicySummary.aspx?PolicyNumber=2031936859")
            
        await asyncio.sleep(5)
        logger.info(f"Landed on: {page.url} (Title: {await page.title()})")
        await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_solano_policy_summary_now.png")
        
        # Check text on policy summary
        body = await page.evaluate("() => document.body.innerText")
        logger.info(f"Page header snippet: {body[:300]}")
        
        # Look for ID Card link or navigate to Summary/IDCardRequest.aspx
        idcard_link = page.locator("a:has-text('ID Card'), a[href*='IDCardRequest']").first
        if await idcard_link.count() > 0:
            logger.info("Clicking ID Card link...")
            await idcard_link.click()
        else:
            logger.info("Navigating directly to Summary/IDCardRequest.aspx...")
            await page.goto("https://natgenagency.com/Summary/IDCardRequest.aspx")
            
        await asyncio.sleep(4)
        logger.info(f"ID Card page URL: {page.url}")
        await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_idcard_page_now.png")
        
        # List all controls, checkboxes, selects, buttons on this page
        elements = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input, select, button, a')).map(el => ({
                tag: el.tagName,
                id: el.id,
                name: el.name,
                type: el.type,
                value: el.value,
                text: el.innerText ? el.innerText.trim() : ''
            })).filter(x => x.text || x.value || x.id || x.name);
        }""")
        for el in elements:
            txt = (str(el['id']) + ' ' + str(el['name']) + ' ' + str(el['text']) + ' ' + str(el['value'])).lower()
            if any(k in txt for k in ['idcard', 'card', 'vehicle', 'print', 'email', 'ford', 'econoline', 'send', 'document']):
                logger.info(f"  Element: {el}")
                
        # Save HTML for inspection
        html = await page.content()
        with open("/opt/renewal-automation-system/data/idcard_page.html", "w") as f:
            f.write(html)
        logger.info("Saved idcard_page.html")

if __name__ == "__main__":
    asyncio.run(run())
