import asyncio
import logging
import os
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("hmf_pdf")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        aspire_page = [page for page in ctx.pages if "maple-tech.com/hmf/directory" in page.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == "fraTocMain"][0]
        
        # Click the printer icon
        cell = main_frame.locator("td[colidx='7']").first
        logger.info(f"Clicking printer cell: {await cell.get_attribute('id')}")
        
        # We listen for both popup and download
        try:
            async with ctx.expect_page(timeout=10000) as new_page_info:
                await cell.click()
            popup = await new_page_info.value
            logger.info(f"Popup opened: {popup.url}")
            await popup.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            logger.info(f"Popup final URL: {popup.url}")
            await popup.screenshot(path="/opt/renewal-automation-system/data/screenshots/hmf_doc_popup.png")
            # If popup has PDF or print dialog
            content = await popup.content()
            logger.info(f"Popup content snippet: {content[:500]}")
        except Exception as e:
            logger.warning(f"No popup opened or error: {e}")
            # Check if modal or iframe opened in aspire_page
            await asyncio.sleep(4)
            await aspire_page.screenshot(path="/opt/renewal-automation-system/data/screenshots/hmf_after_printer_click.png")
            for f in aspire_page.frames:
                logger.info(f"Frame: {f.name}, URL: {f.url}")
                
asyncio.run(run())
