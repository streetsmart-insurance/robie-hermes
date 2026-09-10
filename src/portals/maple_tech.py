"""Maple-Tech Aspire 3.0 carrier portal crawler for Hyundai Marine & Fire (HMF).

Automates policy/application search, document handle discovery, and genuine renewal
PDF extraction directly from Aspire 3.0 API endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from src.config import settings
from src.extractor.quote_parser import extract_policy_total_premium
from src.portals.base_portal import BaseCarrierPortalCrawler, PortalSearchResult
from src.security.secrets_manager import secrets_mgr

logger = logging.getLogger("carrier_portals.maple_tech")

MIN_AUTHENTIC_PDF_BYTES = 10_000

# Document descriptions prioritized for renewal shells
RENEWAL_DOC_PRIORITIES = [
    "POLICY DEC PAGES",
    "POLICY_DEC_PAGES",
    "DEC PAGES",
    "DECLARATION",
    "QUOTATION",
    "RENEWAL QUOTE",
    "POLICY",
]


def select_best_aspire_document(documents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the best renewal document handle from Aspire doc list."""
    if not documents:
        return None

    def _rank(doc: Dict[str, Any]) -> int:
        desc = (doc.get("DocumentDescription") or doc.get("name") or "").upper()
        for idx, pattern in enumerate(RENEWAL_DOC_PRIORITIES):
            if pattern in desc:
                return idx
        return len(RENEWAL_DOC_PRIORITIES) + 1

    sorted_docs = sorted(documents, key=_rank)
    best = sorted_docs[0]
    if _rank(best) <= len(RENEWAL_DOC_PRIORITIES):
        return best
    return None


class MapleTechPortalCrawler(BaseCarrierPortalCrawler):
    """Crawler for Maple-Tech Aspire 3.0 portal (Hyundai Marine & Fire)."""

    def __init__(
        self,
        base_url: str = "https://app.maple-tech.com/hmf",
        headless: bool = True,
        cdp_url: Optional[str] = "http://127.0.0.1:9222",
    ):
        super().__init__(
            carrier_name="Hyundai Marine & Fire",
            base_url=base_url,
            headless=headless,
        )
        self.cdp_url = cdp_url

    async def check_renewal_quote(
        self,
        policy_number: str,
        insured_name: str,
        download_dir: Path,
    ) -> PortalSearchResult:
        """Search Maple-Tech Aspire for the given policy/insured, retrieve renewal dec/quote,
        and extract Policy Total premium.
        """
        download_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "[Maple-Tech HMF] Checking renewal documents for Pol #%s (%s)...",
            policy_number,
            insured_name,
        )

        # 1. Attempt connection via active CDP if available on hermes
        if self.cdp_url:
            try:
                result = await self._run_via_cdp(policy_number, insured_name, download_dir)
                if result and result.success:
                    return result
            except Exception as cdp_err:
                logger.warning("[Maple-Tech HMF] CDP run failed or unavailable: %s. Falling back to browser launch.", cdp_err)

        # 2. Fallback to standalone browser launch with credentials
        return await self._run_via_browser(policy_number, insured_name, download_dir)

    async def _run_via_cdp(
        self,
        policy_number: str,
        insured_name: str,
        download_dir: Path,
    ) -> Optional[PortalSearchResult]:
        """Connect over CDP and retrieve documents from active Aspire session."""
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self.cdp_url)
            context = browser.contexts[0]

            # Find existing Maple-Tech page
            target_page: Optional[Page] = None
            for page in context.pages:
                if "maple-tech" in page.url or "hmf" in page.url:
                    target_page = page
                    break

            if not target_page:
                target_page = await context.new_page()
                await target_page.goto(f"{self.base_url}/directory/index.aspx", timeout=30000)

            result = await self._search_and_extract_from_page(target_page, policy_number, insured_name, download_dir)
            return result

    async def _run_via_browser(
        self,
        policy_number: str,
        insured_name: str,
        download_dir: Path,
    ) -> PortalSearchResult:
        """Launch Playwright, login with secrets_mgr credentials, and retrieve renewal documents."""
        creds = secrets_mgr.get_login_pair("Hyundai Marine & Fire")
        username = creds.get("username") or creds.get("user")
        password = creds.get("password") or creds.get("pass")

        if not username or not password:
            return PortalSearchResult(
                success=False,
                policy_found=False,
                renewal_ready=False,
                status_message="No credentials for Hyundai Marine & Fire / Maple-Tech in Secret Manager",
            )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            try:
                # Login flow
                await page.goto(f"{self.base_url}/directory/index.aspx", timeout=settings.playwright_browser_timeout_ms)
                user_input = page.locator("input#txtUserName, input[name='txtUserName']")
                pass_input = page.locator("input#txtPassword, input[name='txtPassword']")
                if await user_input.count() > 0:
                    await user_input.first.fill(username)
                    await pass_input.first.fill(password)
                    await page.click("input#btnLogin, button#btnLogin")
                    await page.wait_for_timeout(3000)

                result = await self._search_and_extract_from_page(page, policy_number, insured_name, download_dir)
                return result
            finally:
                await browser.close()

    async def _search_and_extract_from_page(
        self,
        page: Page,
        policy_number: str,
        insured_name: str,
        download_dir: Path,
    ) -> PortalSearchResult:
        """Search for policy/application in Aspire and extract documents."""
        clean_num = re.sub(r"-\d+$", "", policy_number)

        # 1. Look for search frame
        search_frame = None
        for fr in page.frames:
            if "GetProgram.aspx" in fr.url or "SEARCH" in fr.url:
                search_frame = fr
                break

        if not search_frame:
            search_url = f"{self.base_url}/datagrid/layouts/SEARCH/GetProgram.aspx?searchtype=A&Release_Session_Scope=true"
            await page.goto(search_url, timeout=30000)
            search_frame = page.main_frame

        # 2. Enter search query
        txt_search = search_frame.locator("input[name*='Search'], input[name*='txtSearch'], input[name*='AppNumber'], input[name*='PolicyNumber']").first
        if await txt_search.count() > 0:
            await txt_search.fill(clean_num)
            btn_search = search_frame.locator("input[value='Search'], input[id*='btnSearch'], button:has-text('Search')").first
            if await btn_search.count() > 0:
                await btn_search.click()
                await page.wait_for_timeout(3000)

        # 3. Locate matching records in grid
        app_id = await self._extract_app_id(search_frame, policy_number, insured_name)
        if not app_id:
            return PortalSearchResult(
                success=True,
                policy_found=False,
                renewal_ready=False,
                status_message=f"Policy/Application #{policy_number} not found in Maple-Tech Aspire",
            )

        # 4. Fetch document list via Aspire REST API
        docs = await self._fetch_doc_list(page, app_id)
        if not docs:
            return PortalSearchResult(
                success=True,
                policy_found=True,
                renewal_ready=False,
                status_message=f"No documents available for App ID {app_id}",
            )

        # 5. Pick the best renewal document
        best_doc = select_best_aspire_document(docs)
        if not best_doc:
            return PortalSearchResult(
                success=True,
                policy_found=True,
                renewal_ready=False,
                status_message=f"No renewal offer or dec page found among {len(docs)} documents for App ID {app_id}",
            )

        # 6. Download the PDF
        handle = best_doc.get("DocumentHandle")
        doc_desc = best_doc.get("DocumentDescription") or "DEC_PAGES"
        clean_desc = re.sub(r"[^A-Za-z0-9_]", "_", doc_desc)
        target_path = download_dir / f"{policy_number}_{clean_desc}.pdf"

        downloaded_bytes = await self._download_doc_bytes(page, handle)
        if not downloaded_bytes or len(downloaded_bytes) < MIN_AUTHENTIC_PDF_BYTES or not downloaded_bytes.startswith(b"%PDF"):
            return PortalSearchResult(
                success=False,
                policy_found=True,
                renewal_ready=False,
                status_message=f"Downloaded file for {doc_desc} was invalid or corrupt (<10KB or not PDF)",
            )

        target_path.write_bytes(downloaded_bytes)
        logger.info("Downloaded authentic PDF to %s (%d bytes)", target_path, len(downloaded_bytes))

        # 7. Extract premium from PDF
        extracted_prem: Optional[float] = None
        try:
            prem_dec = extract_policy_total_premium(downloaded_bytes.decode("latin1", errors="ignore"))
            if prem_dec:
                extracted_prem = float(prem_dec)
        except Exception as p_err:
            logger.warning("Could not extract premium from downloaded PDF: %s", p_err)

        return PortalSearchResult(
            success=True,
            policy_found=True,
            renewal_ready=True,
            document_path=target_path,
            extracted_premium=extracted_prem,
            status_message=f"Retrieved {doc_desc} (${extracted_prem or 0:.2f}) from Maple-Tech Aspire",
        )

    async def _extract_app_id(self, frame, policy_number: str, insured_name: str) -> Optional[str]:
        """Extract AppID from grid results."""
        clean_num = re.sub(r"-\d+$", "", policy_number)
        rows_data = await frame.eval_on_selector_all(
            "table tr, div.grid-row",
            """els => els.map(e => ({
                text: e.innerText,
                html: e.innerHTML,
                onclick: e.getAttribute('onclick') || ''
            }))""",
        )

        for row in rows_data:
            text = row.get("text", "")
            html = row.get("html", "")
            if clean_num in text or (insured_name and insured_name.split()[0] in text):
                match = re.search(r"goToRecord\s*\(\s*['\"]?(-?\d+)['\"]?", html)
                if match:
                    return match.group(1)
                match = re.search(r"AppID[=']\s*(-?\d+)", html, re.IGNORECASE)
                if match:
                    return match.group(1)
                match = re.search(r"appid=(-?\d+)", html, re.IGNORECASE)
                if match:
                    return match.group(1)

        return None

    async def _fetch_doc_list(self, page: Page, app_id: str) -> List[Dict[str, Any]]:
        """Query Aspire REST endpoint for document list of an app."""
        docs_url = f"{self.base_url}/api/v1/docs/app/{app_id}"
        try:
            resp = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url);
                    if (!r.ok) return [];
                    return await r.json();
                }""",
                docs_url,
            )
            return resp if isinstance(resp, list) else []
        except Exception as err:
            logger.error("Error querying Aspire doc list for app %s: %s", app_id, err)
            return []

    async def _download_doc_bytes(self, page: Page, handle: str) -> Optional[bytes]:
        """Download document content bytes via Aspire handle."""
        dl_url = f"{self.base_url}/api/v1/docs/{handle}"
        try:
            hex_data = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url);
                    if (!r.ok) return null;
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let hex = '';
                    for (let i = 0; i < bytes.length; i++) {
                        hex += bytes[i].toString(16).padStart(2, '0');
                    }
                    return hex;
                }""",
                dl_url,
            )
            if hex_data:
                return bytes.fromhex(hex_data)
            return None
        except Exception as err:
            logger.error("Error downloading doc handle %s: %s", handle, err)
            return None
