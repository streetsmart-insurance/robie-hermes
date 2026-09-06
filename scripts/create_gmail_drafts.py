import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def create_drafts():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        drafts_to_create = [
            {
                "to": "midlanticoffice@merchantsgroup.com",
                "cc": "msantiago@merchantsgroup.com, lenin@streetsmart.insurance",
                "subject": "Policy Change Request Follow-Up: Cardone Electric LLC - Policy # CAPI075976",
                "body": (
                    "Hello Merchants Mid-Atlantic Underwriting Team,\n\n"
                    "We are following up on the Commercial Auto policy change request submitted on August 25, 2026, for Cardone Electric LLC (Policy # CAPI075976).\n\n"
                    "Details of Requested Change:\n"
                    "• Action: Remove Driver\n"
                    "• Driver Name: Alexander J Rutledge\n"
                    "• DOB: 03/05/2003\n"
                    "• Driver's License: R94870197103032\n"
                    "• Requested Effective Date: 08/25/2026\n\n"
                    "Could you please confirm if this endorsement has been issued, and provide a copy of the endorsement declaration and any resulting invoice or premium adjustment?\n\n"
                    "Thank you,\n"
                    "StreetSmart Insurance Team\n"
                    "lenin@streetsmart.insurance | (732) 462-8343\n"
                )
            },
            {
                "to": "stephanie.tower@rtspecialty.com",
                "cc": "quickhome@allrisks.com, eimy@streetsmart.insurance",
                "subject": "Policy Change Request Follow-Up: Shoreline Builders LLC - Policy # MGL0200092 / MXL0446256",
                "body": (
                    "Hello Stephanie and RT Specialty Team,\n\n"
                    "We are following up on the policy change request submitted on August 18, 2026, for Shoreline Builders LLC under General Liability policy # MGL0200092 and Excess policy # MXL0446256.\n\n"
                    "Could you please provide an update on the status of this endorsement / documentation, and share the updated declarations or confirmation for our records?\n\n"
                    "Thank you,\n"
                    "StreetSmart Insurance Team\n"
                    "eimy@streetsmart.insurance | (732) 462-8343\n"
                )
            }
        ]

        for d in drafts_to_create:
            logger.info(f"Creating draft for: {d['to']} - {d['subject']}...")
            
            # Click hamburger menu if compose not visible
            compose_btn = page.locator('div[gh="cm"], div[role="button"]:has-text("Compose")').first
            if not await compose_btn.is_visible():
                menu = page.locator('button[aria-label="Main menu"], div[aria-label="Main menu"]').first
                if await menu.is_visible():
                    await menu.click()
                    await asyncio.sleep(1)
            
            await compose_btn.click()
            await asyncio.sleep(2)
            
            # Fill To
            to_input = page.locator('input[aria-label="To recipients"], input[peoplekit-id]').first
            if not await to_input.is_visible():
                to_input = page.locator('input[role="combobox"]').first
            await to_input.fill(d["to"])
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)

            # Fill CC if visible or click Cc
            if d.get("cc"):
                cc_btn = page.locator('span[aria-label="Add Cc recipients"], span:has-text("Cc")').first
                if await cc_btn.is_visible():
                    await cc_btn.click()
                    await asyncio.sleep(0.5)
                cc_input = page.locator('input[aria-label="Cc recipients"]').first
                if await cc_input.is_visible():
                    for cc_email in d["cc"].split(","):
                        await cc_input.fill(cc_email.strip())
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.3)

            # Fill Subject
            subj_input = page.locator('input[name="subjectbox"]')
            await subj_input.fill(d["subject"])
            await asyncio.sleep(0.5)

            # Fill Body in editable div
            body_box = page.locator('div[role="textbox"][aria-label*="Message Body"]').first
            await body_box.click()
            # Insert at beginning of body before signature
            await page.keyboard.type(d["body"])
            await asyncio.sleep(1)

            # Close compose dialog to save as draft
            close_btn = page.locator('img[aria-label="Save & close"], img[alt="Close"]').first
            if await close_btn.is_visible():
                await close_btn.click()
            else:
                await page.keyboard.press("Escape")
            await asyncio.sleep(2)
            logger.info(f"Saved draft for {d['to']}!")

        await page.close()
        logger.info("All drafts created successfully!")

asyncio.run(create_drafts())
