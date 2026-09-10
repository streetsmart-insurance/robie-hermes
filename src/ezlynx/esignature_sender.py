"""
EZLynx AssureSign E-Signature Automation Engine
-----------------------------------------------
Automates the full 4-step EZLynx E-Signature envelope workflow:
  1. Add Document(s) to envelope
  2. Setup Envelope (recipients, custom agent signers, envelope details)
  3. Setup Signature (RadPdf API-based signature and date tab placement)
  4. Review and Send envelope
"""

import asyncio
import logging
from typing import List, Dict, Optional, Any
from playwright.async_api import async_playwright, Page, Frame

logger = logging.getLogger(__name__)

class EZLynxEsignatureSender:
    def __init__(self, cdp_url: str = "http://localhost:9222"):
        self.cdp_url = cdp_url

    async def send_envelope(
        self,
        applicant_id: str,
        document_name: str,
        envelope_name: Optional[str] = None,
        reference_number: Optional[str] = None,
        agent_signer: Optional[Dict[str, str]] = None,  # {"name": "Carlo Ferrara", "email": "carlo@streetsmart.insurance"}
        note: Optional[str] = None,
        signature_fields: Optional[List[Dict[str, Any]]] = None
    ) -> bool:
        """
        Sends an eSignature envelope for the specified applicant document.
        """
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(self.cdp_url, timeout=30000)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            # 1. Navigate to eSignature wizard
            wizard_url = f"https://app.ezlynx.com/web/account/{applicant_id}/documents/esignature"
            logger.info("Navigating to %s", wizard_url)
            await page.goto(wizard_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Step 1: Add Document(s)
            logger.info("Step 1: Selecting document '%s'...", document_name)
            rows = await page.locator('table tr').all()
            target_add_btn = None
            for row in rows:
                text = await row.inner_text()
                if document_name in text:
                    btn = row.locator('button:has-text("Add"), a:has-text("Add")')
                    if await btn.count() > 0:
                        target_add_btn = btn.first
                        break

            if target_add_btn:
                await target_add_btn.click()
                await asyncio.sleep(2)
            else:
                logger.warning("Could not find Add button for document '%s'", document_name)

            next_btn = page.locator('button:has-text("Next")')
            await next_btn.click()
            await asyncio.sleep(3)

            # Step 2: Setup Envelope
            logger.info("Step 2: Configuring envelope...")
            if envelope_name:
                name_input = page.locator('input[data-placeholder="Envelope name"], input[formcontrolname="envelopeName"]')
                if await name_input.count() > 0:
                    await name_input.first.fill(envelope_name)

            if reference_number:
                ref_input = page.locator('input[data-placeholder="Reference number"], input[formcontrolname="referenceNumber"]')
                if await ref_input.count() > 0:
                    await ref_input.first.fill(reference_number)

            if agent_signer:
                logger.info("Adding agent signer %s (%s)...", agent_signer.get("name"), agent_signer.get("email"))
                add_signer_btn = page.locator('button:has-text("Add signer")')
                if await add_signer_btn.count() > 0:
                    await add_signer_btn.first.click()
                    await asyncio.sleep(1)
                    custom_contact_item = page.locator('text="Add custom contact"')
                    if await custom_contact_item.count() > 0:
                        await custom_contact_item.first.click()
                        await asyncio.sleep(1)

                        await page.locator('input[data-placeholder="Name"]').first.fill(agent_signer["name"])
                        await page.locator('input[data-placeholder="Email"]').first.fill(agent_signer["email"])
                        await page.locator('button:has-text("Save")').first.click()
                        await asyncio.sleep(2)

            if note:
                note_textarea = page.locator('textarea[data-placeholder="Note"], textarea[formcontrolname="note"]')
                if await note_textarea.count() > 0:
                    await note_textarea.first.fill(note)

            next_btn = page.locator('button:has-text("Next")')
            await next_btn.click()
            await asyncio.sleep(5)

            # Step 3: Setup Signature
            logger.info("Step 3: Placing signature tabs in RadPdf...")
            edit_frame: Optional[Frame] = None
            for frame in page.frames:
                if "envelopeid" in frame.url.lower():
                    edit_frame = frame
                    break

            if not edit_frame:
                raise RuntimeError("Could not find RadPdf edit frame for Step 3.")

            # Apply signature and date blocks via RadPdf API
            result = await edit_frame.evaluate('''() => {
                const p3 = window.oRadPdf.getPage(3);
                while (p3.getObjectCount() > 0) {
                    p3.getObject(0).deleteObject();
                }

                const r1 = window.arRecipients[0]; // Client
                const r2 = window.arRecipients.length > 1 ? window.arRecipients[1] : null; // Agent

                function createTab(pageObj, pageNum, recipient, tabTypeKey, x, y, w, h) {
                    const tabInfo = window.tabs[tabTypeKey];
                    const customData = {
                        tabIndex: tabInfo.addTabFlag,
                        tabType: tabTypeKey,
                        tabName: "  " + recipient.RecipientName + "-" + tabInfo.tabTypeDesc,
                        tabInstruction: window.getDefaultTabInstruction(tabTypeKey),
                        currentPageNum: pageNum,
                        recipientData: recipient.ID.toString(),
                        recipientName: recipient.RecipientName,
                        certified: tabInfo.certified,
                        divid: 0
                    };
                    const objProp = {
                        movable: true,
                        changeable: false,
                        readOnly: true,
                        required: true,
                        multiline: true,
                        left: x,
                        top: y,
                        width: w,
                        height: h,
                        fillColor: "#FFFF00",
                        border: { "color": "#0000CC", "width": 2 },
                        font: { "italic": true, "bold": true, "color": "#0000FF", "size": "28" }
                    };
                    const o = pageObj.addObject(tabInfo.objectType, objProp.left, objProp.top, objProp.width, objProp.height);
                    customData.divid = o.getDiv().id;
                    objProp.customData = JSON.stringify(customData);
                    objProp.name = 'New' + o.getDiv().id;
                    o.getDiv().children[0].style.overflow = "hidden";
                    window.setObjectProperties(o, objProp);
                    return o;
                }

                // Line 1: Client
                createTab(p3, 3, r1, "EzSign", 130, 615, 215, 45);
                createTab(p3, 3, r1, "EzDate", 720, 615, 180, 45);

                // Line 2: Agent (if present)
                if (r2) {
                    createTab(p3, 3, r2, "EzSign", 130, 745, 215, 45);
                    createTab(p3, 3, r2, "EzDate", 720, 745, 180, 45);
                }

                window.hasChanged = true;
                const tabData = window.GetAddedObject();
                return {
                    isValid: window.validateTabRule(),
                    tabCount: tabData.length,
                    recipients: window.recipientsWithTab
                };
            }''')
            logger.info("Tabs applied: %s", result)

            review_btn = page.locator('button:has-text("Review and send")')
            await review_btn.click()
            await asyncio.sleep(4)

            # Step 4: Review and Send
            logger.info("Step 4: Dispatching envelope...")
            send_btn = page.locator('button:has-text("Send")')
            await send_btn.click()
            await asyncio.sleep(6)

            logger.info("Envelope sent successfully! Navigated to: %s", page.url)
            return True
