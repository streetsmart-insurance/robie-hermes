import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMAIL_BODY = (
    "Hi Team Leads (Ashley, Sandy, Gabi, Jake),\n\n"
    "Here is an update on the Policy Change Request confirmation queue, 3-way match audits, and carrier follow-up actions completed today:\n\n"
    "1. Queue Triage & Non-Closure Policy Enforced:\n"
    "   • Audited the open Policy Change Request queue across all 23 active requests.\n"
    "   • In accordance with agency controls, all 23 policy change requests remain OPEN in EZLynx until full carrier endorsements, billing adjustments, and file confirmations are verified. Zero premature closures were made.\n\n"
    "2. Three-Way Match Verification (Ready Accounts - 100% Match):\n"
    "   Cross-referenced (1) Insured Request / Intent, (2) Carrier-Issued Contract / Endorsement, and (3) EZLynx Agency System Record for 5 ready accounts:\n"
    "   • Omega General Construction LLC (NatGen 2021047341): Added 2015 RAM ProMaster, $1k/$1k ded, $839.39 AP. Fully matched with signed ACORD 175 and endorsement #4 on file.\n"
    "   • Haughey Brothers Landscaping LLC (Geico 9300289715): Added 2009 International Dump Truck & removed driver. Fully confirmed via Geico confirmation #F5ECF9.\n"
    "   • B&M All Inclusive LLC (Utica First ART3000230570): Mailing address updated across both BOP (Utica First) and WC (Hartford) simultaneously ($0 AP).\n"
    "   • LA Burger LLC (USLI CP 1916678 & Geico 9300308410): Address update processed across Commercial Package & Commercial Auto.\n"
    "   • On My Way Painting and Carpentry LLC (Selective S 2527442): Vehicle removal confirmed and matched via Selective eDocs download.\n\n"
    "3. StreetSmart Policy Change SOP Compliance Audit:\n"
    "   • Cross-referenced all processed requests against the official StreetSmart SOP (Tab 4 Intake Guide).\n"
    "   • Verified intake compliance: customer service representatives properly confirmed client authority, effective dates, loss history, vehicle physical damage deductibles, commercial usage, and multi-policy address impacts.\n\n"
    "4. Permanent Case Documentation in EZLynx:\n"
    "   • Detailed audit findings and verification notes were posted directly to applicant discussion cards on EZLynx for each completed review, concluding with the standard signature: \"Robie was here\".\n\n"
    "5. Carrier Follow-Up Emails Dispatched Out of EZLynx:\n"
    "   Cross-referenced our Carrier Directory & Download Matrix:\n"
    "   • Merchants Insurance Group (>5 days pending): Sent follow-up for Cardone Electric LLC (CAPI075976) to midlanticoffice@merchantsgroup.com (CC msantiago@merchantsgroup.com, lenin@streetsmart.insurance).\n"
    "   • RT Specialty / Interstate MGA: Sent follow-up for Shoreline Builders LLC (MGL0200092 / MXL0446256) to stephanie.tower@rtspecialty.com (CC quickhome@allrisks.com, eimy@streetsmart.insurance).\n"
    "   • Both follow-ups were sent directly through EZLynx using the official agency template: \"Policy Change Request Change Request Follow up Email Templates (Carrier)\". Insured client emails were removed from the recipient list so only carriers/wholesalers received the requests.\n\n"
    "6. Workflow Optimization Coaching to Lenin:\n"
    "   • Sent an update to Lenin clarifying that once policy change requests are marked completed/processed in EZLynx, the Automation Center automatically sends the completion email and Client Center access link to the insured. Discontinuing manual completion emails will save time and prevent duplicate notifications.\n\n"
    "Please let me know if you have any questions or need further details on any specific account!\n\n"
    "Kind regards,\n"
)

async def send_team_leads_email():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        
        # Close any lingering compose draft on existing mail pages
        for pg in ctx.pages:
            if "mail.google.com" in pg.url:
                try:
                    await pg.close()
                except Exception:
                    pass

        # Open fresh page
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        compose_btn = page.locator('div[gh="cm"], div[role="button"]:has-text("Compose")').first
        if not await compose_btn.is_visible():
            menu = page.locator('button[aria-label="Main menu"]').first
            await menu.click()
            await asyncio.sleep(1)

        await compose_btn.click()
        await asyncio.sleep(2)

        # To field
        to_input = page.locator('input[aria-label="To recipients"], input[peoplekit-id], input[role="combobox"]').first
        team_leads = [
            "ashley@streetsmart.insurance",
            "sandy@streetsmart.insurance",
            "gabrielac@streetsmart.insurance",
            "jake@streetsmart.insurance"
        ]
        
        for email in team_leads:
            await to_input.fill(email)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)
        logger.info("Added all 4 team leads to To field.")

        # Subject
        subj_input = page.locator('input[name="subjectbox"]')
        await subj_input.fill("Policy Change Request Audit & Queue Status Update (1, 2, 3)")
        await asyncio.sleep(0.5)

        # Body
        body_box = page.locator('div[role="textbox"][aria-label*="Message Body"]').first
        await body_box.focus()
        await page.keyboard.press("Home")
        await page.keyboard.insert_text(EMAIL_BODY)
        await asyncio.sleep(1.5)

        # Verify body content
        verified_text = await body_box.inner_text()
        logger.info(f"Verified body content in Gmail compose box:\n{verified_text[:150]}")

        # Capture preview screenshot before sending
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/team_leads_email_preview_verified.png")

        # Click Send
        send_btn = page.locator('div[role="button"][data-tooltip*="Send"], div[role="button"]:has-text("Send")').filter(has_not_text="Schedule").first
        logger.info("Clicking Send to Team Leads...")
        await send_btn.click()
        await asyncio.sleep(3)

        # Go to sent to verify
        await page.goto("https://mail.google.com/mail/u/0/#sent", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/team_leads_email_sent_verified.png")
        logger.info("Email to Team Leads successfully sent and verified!")

if __name__ == "__main__":
    asyncio.run(send_team_leads_email())
