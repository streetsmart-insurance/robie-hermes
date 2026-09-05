import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright

logger = logging.getLogger("ezlynx_document_uploader")

FOLDER_ROUTING = {
    "renewal": ["Renewal Offers/Declarations", "Renewal Offers", "Renewals", "Documents"],
    "quote": ["Renewal Offers/Declarations", "Renewal Offers", "Renewals", "Documents"],
    "non renewal": ["Cancellations/NonRenewals/Reinstatements", "Cancellations/Non-Renewals", "Non-Renewals", "Documents"],
    "cancellation": ["Cancellations/NonRenewals/Reinstatements", "Cancellations/Non-Renewals", "Non-Renewals", "Documents"],
    "loss runs": ["Loss Runs", "Prior Policies & Loss Runs", "Loss History", "Documents"],
    "loss run": ["Loss Runs", "Prior Policies & Loss Runs", "Loss History", "Documents"],
    "application": ["Applications", "Renewal Applications", "Documents"],
    "correspondence": ["Documents", "Correspondence"],
}

LABEL_ROUTING = {
    "renewal": "Renewal Offer",
    "quote": "Renewal Offer",
    "non renewal": "Non Renewal",
    "cancellation": "Non Renewal",
    "loss runs": "Loss Runs",
    "loss run": "Loss Runs",
    "application": "Application",
    "correspondence": "Correspondence",
}

SUFFIX_ROUTING = {
    "renewal": "Renewal Offer.pdf",
    "quote": "Renewal Offer.pdf",
    "non renewal": "Non Renewal.pdf",
    "cancellation": "Non Renewal.pdf",
    "loss runs": "Loss Runs.pdf",
    "loss run": "Loss Runs.pdf",
    "application": "Renewal Application.pdf",
}


class EZLynxDocumentUploader:
    """Automates uploading documents to the Documents tab in EZLynx via Playwright Chrome CDP,

    adhering to StreetSmart agency conventions:
    - Clean naming: '{policy_number} Renewal Offer.pdf', '{policy_number} Loss Runs.pdf', etc.
    - Policy association: Automatically selected from the policy dropdown.
    - Target folder: Routes to Renewal Offers/Declarations, Loss Runs, or Cancellations/NonRenewals.
    """

    def __init__(
        self,
        cdp_url: Optional[str] = "http://localhost:9222",
        storage_state_path: Optional[Path] = None
    ):
        self.cdp_url = cdp_url
        self.storage_state_path = storage_state_path or Path("data/ezlynx_storage_state.json")

    async def upload_document(
        self,
        applicant_id: str,
        file_path: Path,
        policy_number: Optional[str] = None,
        doc_type: str = "renewal",  # "renewal", "non renewal", "loss runs", "application"
        doc_title: Optional[str] = None,
        label_to_apply: Optional[str] = None,
        target_folder: Optional[str] = None
    ) -> Dict[str, Any]:
        """Uploads a single document, formats name per agency standards, links to policy,

        and applies existing system labels if present (without creating new ones).
        """
        file_path = Path(file_path).resolve()
        if not file_path.is_file():
            return {"success": False, "error": f"File not found: {file_path}"}

        norm_doc_type = doc_type.strip().lower()

        # Resolve candidate target folders
        if target_folder:
            candidate_folders = [target_folder]
        else:
            candidate_folders = FOLDER_ROUTING.get(norm_doc_type, ["Renewal Offers/Declarations", "Documents"])

        # Format document name: e.g. "02TRM066190-01 Renewal Offer.pdf" or "02TRM066190-01 Loss Runs.pdf"
        clean_pnum = policy_number.strip() if policy_number else ""
        if doc_title:
            target_doc_name = f"{doc_title}.pdf" if not doc_title.endswith(".pdf") else doc_title
        elif clean_pnum:
            suffix = SUFFIX_ROUTING.get(norm_doc_type, f"{norm_doc_type.title()}.pdf")
            target_doc_name = f"{clean_pnum} {suffix}"
        else:
            target_doc_name = file_path.name

        # Default system label if not explicitly specified
        if not label_to_apply:
            label_to_apply = LABEL_ROUTING.get(norm_doc_type, "Renewal Offer")

        async with async_playwright() as p:
            browser = None
            is_standalone = False
            try:
                # 1. Use EZLynxSessionManager to get authenticated context (auto-handles login/2FA)
                try:
                    from src.ezlynx.session_manager import EZLynxSessionManager
                    mgr = EZLynxSessionManager(
                        storage_state_path=self.storage_state_path,
                        cdp_url=self.cdp_url
                    )
                    browser, context = await mgr.get_authenticated_context(p, headless=True)
                    is_standalone = True
                except Exception as sess_err:
                    logger.warning(f"EZLynxSessionManager error: {sess_err}, trying direct storage_state or CDP fallback...")
                    if self.storage_state_path and Path(self.storage_state_path).is_file():
                        browser = await p.chromium.launch(
                            headless=True,
                            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                        )
                        context = await browser.new_context(
                            storage_state=str(self.storage_state_path),
                            viewport={"width": 1440, "height": 900}
                        )
                        is_standalone = True
                    elif self.cdp_url:
                        browser = await p.chromium.connect_over_cdp(self.cdp_url)
                        context = browser.contexts[0] if browser.contexts else await browser.new_context()

                if not browser or not context:
                    return {"success": False, "error": "Neither EZLynxSessionManager nor CDP connection could be established."}

                page = await context.new_page()

                docs_url = f"https://app.ezlynx.com/web/account/{applicant_id}/documents"
                logger.info(f"Navigating to {docs_url} for applicant {applicant_id}...")
                await page.goto(docs_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)

                # If redirected to login, re-authenticate on the spot
                if "auth/account/login" in page.url.lower():
                    logger.warning(f"Redirected to login while accessing {docs_url}. Performing in-line login...")
                    from src.ezlynx.session_manager import EZLynxSessionManager
                    mgr = EZLynxSessionManager(storage_state_path=self.storage_state_path)
                    await mgr._perform_login(context)
                    await page.goto(docs_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(4)

                # Optional: Navigate into target folder if it exists on page
                for folder_candidate in candidate_folders:
                    folder_cell = page.locator(f"td:has-text('{folder_candidate}'), a:has-text('{folder_candidate}')")
                    if await folder_cell.count() > 0:
                        logger.info(f"Navigating into folder: {folder_candidate}")
                        await folder_cell.first.click()
                        await asyncio.sleep(3)
                        break
                    folder_row = page.locator(f"tr:has-text('{folder_candidate}')")
                    if await folder_row.count() > 0:
                        logger.info(f"Double-clicking folder row: {folder_candidate}")
                        await folder_row.first.dblclick()
                        await asyncio.sleep(3)
                        break

                # Close notification drawer if open
                close_btn = page.locator("mat-icon:has-text('close'), button:has-text('close')")
                for i in range(await close_btn.count()):
                    box = await close_btn.nth(i).bounding_box()
                    if box and box["x"] > 750 and box["y"] < 250:
                        await close_btn.nth(i).click()
                        await asyncio.sleep(1)
                        break

                # Click #add-action button
                add_btn = page.locator("#add-action")
                await add_btn.wait_for(state="visible", timeout=15000)
                await add_btn.click()
                await asyncio.sleep(1)

                # Click Upload item in dropdown menu
                upload_item = page.locator(".mat-mdc-menu-item:has-text('Upload')")
                await upload_item.wait_for(state="visible", timeout=10000)
                await upload_item.click()
                await asyncio.sleep(3)

                # Locate upload modal iframe
                frame = page.frame(url=lambda u: "documentactions/upload" in u)
                if not frame:
                    for _ in range(10):
                        await asyncio.sleep(1)
                        frame = page.frame(url=lambda u: "documentactions/upload" in u)
                        if frame:
                            break

                if not frame:
                    await page.close()
                    return {"success": False, "error": "Upload dialog iframe could not be located"}

                # Set file on input
                file_input = frame.locator("input[type=file]")
                await file_input.wait_for(state="attached", timeout=10000)
                await file_input.set_input_files([str(file_path)])
                logger.info(f"Attached file: {file_path.name}")
                await asyncio.sleep(2)

                # Set custom Document Name input (#file-desc-0)
                name_input = frame.locator("#file-desc-0")
                if await name_input.count() > 0:
                    await name_input.fill(target_doc_name)
                    logger.info(f"Filled document title: {target_doc_name}")

                # Associate with policy if provided (#selected-policyor-application-0)
                if clean_pnum:
                    policy_select = frame.locator("#selected-policyor-application-0")
                    if await policy_select.count() > 0:
                        await policy_select.click()
                        await asyncio.sleep(1)
                        first_token = clean_pnum.split()[0] if " " in clean_pnum else clean_pnum
                        policy_opt = frame.locator(f"mat-option:has-text('{clean_pnum}'), mat-option:has-text('{first_token}')")
                        if await policy_opt.count() > 0:
                            await policy_opt.first.click()
                            logger.info(f"Linked document to policy matching: {clean_pnum}")
                        else:
                            logger.warning(f"No policy option matched: {clean_pnum}")
                        await asyncio.sleep(1)

                # Apply existing system label if available (do NOT create new ones)
                applied_label = None
                if label_to_apply:
                    try:
                        label_filter = frame.locator("input[aria-label='filter'], input[placeholder*='Label'], #mat-input-1")
                        if await label_filter.count() > 0:
                            await label_filter.first.click(force=True)
                            await label_filter.first.fill(label_to_apply)
                            await asyncio.sleep(1)
                            # Check for exact existing system option
                            mat_opt = frame.locator(f"mat-option:has-text('{label_to_apply}'), .mat-mdc-option:has-text('{label_to_apply}')")
                            if await mat_opt.count() > 0:
                                await mat_opt.first.click()
                                applied_label = label_to_apply
                                logger.info(f"Applied existing system label: {label_to_apply}")
                            else:
                                logger.info(f"Label '{label_to_apply}' not found in system options; skipping without creating new one.")
                                await label_filter.first.fill("")
                    except Exception as label_err:
                        logger.warning(f"Could not apply label {label_to_apply}: {label_err}")

                # Click Upload submit button
                upload_submit_btn = frame.locator("button:has-text('Upload')")
                await upload_submit_btn.wait_for(state="visible", timeout=10000)
                await upload_submit_btn.click()
                logger.info("Submitted document upload to EZLynx.")
                await asyncio.sleep(6)

                screenshot_dir = Path("data/screenshots")
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = screenshot_dir / f"doc_uploaded_{applicant_id}_{clean_pnum}.png"
                await page.screenshot(path=str(screenshot_path))

                await page.close()
                return {
                    "success": True,
                    "applicant_id": applicant_id,
                    "document_name": target_doc_name,
                    "policy_number": clean_pnum,
                    "applied_label": applied_label,
                    "screenshot_path": str(screenshot_path)
                }
            except Exception as e:
                logger.error(f"Failed to upload document for applicant {applicant_id}: {e}", exc_info=True)
                return {"success": False, "error": str(e)}
            finally:
                if is_standalone and browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
