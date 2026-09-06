import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def send_shoreline():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        # Open compose directly for Shoreline Builders (ID: 48641902)
        await page.goto("https://app.ezlynx.com/web/email/compose/48641902", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        
        # 1. Remove client chip in To field
        to_field = page.locator('mat-form-field:has-text("To")').first
        remove_btns = to_field.locator('.mat-mdc-chip-remove')
        count = await remove_btns.count()
        for i in range(count):
            await remove_btns.first.click()
            await asyncio.sleep(0.5)
        logger.info("Removed client chip from To field.")

        # 2. Add Carrier To: stephanie.tower@rtspecialty.com
        to_input = page.locator('#mat-mdc-chip-list-input-1')
        await to_input.click()
        await to_input.type("stephanie.tower@rtspecialty.com", delay=40)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        await page.keyboard.press("Escape")
        logger.info("Added carrier To recipient.")

        # 3. Open CC and add recipients
        btn_cc = page.locator('#btnCC')
        if await btn_cc.is_visible():
            await btn_cc.click()
            await asyncio.sleep(1)

        cc_input = page.locator('#mat-mdc-chip-list-input-2')
        await cc_input.click()
        await cc_input.type("quickhome@allrisks.com", delay=40)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        await cc_input.type("eimy@streetsmart.insurance", delay=40)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        await page.keyboard.press("Escape")
        logger.info("Added CC recipients.")

        # 4. Select Email Template
        select = page.locator('#mat-select-0').first
        await select.click()
        await asyncio.sleep(1.5)
        opt = page.locator('mat-option').filter(has_text='Policy Change Request Change Request Follow up Email Templates (Carrier)').first
        await opt.scroll_into_view_if_needed()
        await opt.click()
        await asyncio.sleep(2)
        logger.info("Selected carrier follow-up template.")

        # 5. Set Subject
        subj_input = page.locator('#subject')
        await subj_input.fill("Shoreline Builders LLC - MGL0200092 / MXL0446256 - Policy Change Request Follow Up")
        await asyncio.sleep(0.5)

        # 6. Update body via CKEditor
        body_content = '''<p dir="ltr">Hello Stephanie and RT Specialty Team,</p>

<p dir="ltr">We hope you are well!</p>

<p dir="ltr">At your earliest convenience, please forward the confirmation of the policy change request submitted on August 18, 2026, for our file:</p>

<p dir="ltr">
• <strong>Insured:</strong> Shoreline Builders LLC<br />
• <strong>Policy Numbers:</strong> MGL0200092 (General Liability) &amp; MXL0446256 (Excess Liability)<br />
• <strong>Carrier / Wholesaler:</strong> RT Specialty / Interstate Insurance Management<br />
• <strong>Change Requested:</strong> Policy change / endorsement confirmation documentation per request submitted on 08/18/2026
</p>

<p dir="ltr">Please provide the updated endorsement declaration and any resulting billing adjustments or premium changes.</p>

<p dir="ltr">Should you have any questions please feel free to email me back.</p>'''

        await page.evaluate(f'''(body) => {{
            const editor = window.CKEDITOR.instances['editor-1'];
            const curData = editor.getData();
            const sigIdx = curData.indexOf('<table');
            const sig = sigIdx !== -1 ? curData.substring(sigIdx) : '';
            editor.setData(body + '<br /><br />' + sig);
        }}''', body_content)
        await asyncio.sleep(2)
        logger.info("Body updated with CKEditor.")

        # Dismiss any open popups
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)

        # Screenshot ready email
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/ezlynx_shoreline_email_ready.png")
        logger.info("Saved ready screenshot.")

        # 7. Click Send
        send_btn = page.locator('button:has-text("Send")').first
        logger.info("Clicking Send in EZLynx...")
        await send_btn.click()
        await asyncio.sleep(4)
        logger.info("Send clicked!")

asyncio.run(send_shoreline())
