import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("search_policy")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        
        aspire_page = None
        for page in ctx.pages:
            if "maple-tech.com/hmf/directory" in page.url:
                aspire_page = page
                break
                
        if not aspire_page:
            logger.error("No aspire directory page found")
            return
            
        toc_frame = None
        for frame in aspire_page.frames:
            if frame.name == "fraToc":
                toc_frame = frame
                break
                
        if not toc_frame:
            logger.error("No fraToc found")
            return
            
        logger.info("Looking for 'Policy Records' in fraToc...")
        pr = toc_frame.locator("text='Policy Records'").first
        await pr.click()
        await asyncio.sleep(4)
        
        await aspire_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/hmf_policy_records_form.png")
        
        # Check fraTocMain
        main_frame = None
        for frame in aspire_page.frames:
            if frame.name == "fraTocMain":
                main_frame = frame
                break
                
        if main_frame:
            logger.info(f"fraTocMain URL: {main_frame.url}")
            inputs = await main_frame.locator("input:visible, select:visible").all()
            for i, inp in enumerate(inputs):
                tag = await inp.evaluate("el => el.tagName")
                iid = await inp.get_attribute("id") or ""
                iname = await inp.get_attribute("name") or ""
                itype = await inp.get_attribute("type") or ""
                logger.info(f"Input {i}: tag={tag}, type={itype}, id={iid}, name={iname}")

if __name__ == "__main__":
    asyncio.run(run())
