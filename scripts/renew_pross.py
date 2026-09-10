import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("renew_pross")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            applicant_id = "151445306"
            logger.info(f"Navigating to policies page for applicant {applicant_id}...")
            await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/policies", wait_until="domcontentloaded")
            await asyncio.sleep(4)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_policies_init.png")
            
            # Click on the policy row or card for WC5-33S-B1X2Z3-025
            pol_elem = page.locator("text=WC5-33S-B1X2Z3-025").first
            logger.info("Clicking on policy WC5-33S-B1X2Z3-025...")
            await pol_elem.click()
            await asyncio.sleep(4)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_policy_details.png")
            
            # Click Service dropdown
            logger.info("Looking for Service dropdown...")
            service_btn = page.locator("button:has-text('Service'), a:has-text('Service')").first
            await service_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_service_menu.png")
            
            # Click Renew option
            renew_opt = page.locator("a:has-text('Renew'), button:has-text('Renew')").first
            logger.info("Clicking Renew option...")
            await renew_opt.click()
            await asyncio.sleep(6)
            
            logger.info(f"Renewal Page URL: {page.url}")
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_renew_page.png")
            
            # Check inputs on Renew page
            written_prem = page.locator("input#WrittenPremium, input[name='WrittenPremium'], input[placeholder*='Premium' i]").first
            if await written_prem.count() > 0:
                val = await written_prem.input_value()
                logger.info(f"Current Written Premium field value: {val}")
                if not val or val.strip() in ["", "$0", "$0.00", "0", "0.00"]:
                    logger.info("Setting written premium to 8893.00...")
                    await written_prem.fill("8893.00")
            
            # Find #RenewPolicyBtn
            renew_btn = page.locator("#RenewPolicyBtn")
            logger.info(f"Found #RenewPolicyBtn: {await renew_btn.count() > 0}")
            if await renew_btn.count() > 0:
                logger.info("Submitting #RenewPolicyBtn...")
                await renew_btn.click()
                await asyncio.sleep(6)
                logger.info(f"Post-Submit URL: {page.url}")
                await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_renew_submitted.png")
            
            # Navigate back to policies page to verify renewal shell
            await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/policies", wait_until="domcontentloaded")
            await asyncio.sleep(4)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/pross_policies_final_proof.png")
            logger.info("Captured final policies proof screenshot.")
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
