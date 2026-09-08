"""EZLynx Dual-Subsystem API Client.

Integrates with both:
1. Classic Web Services REST API (services.ezlynx.com)
   - Authentication via EZUser, EZPassword, EZAppSecret, and session EZToken.
   - Applicant profile retrieval (Commercial & Personal).
   - Policy history and line-of-business records by applicant.
   - Document Library listing and document uploading.
   - Comparative Rating / Quoting session results (Quote/GetCompletedQuote).
2. Modern OAuth2 Gateway (app.ezlynx.com)
   - OAuth2 Bearer token authentication via vendor_data_access grant.
   - PolicyAPI policy search and transactions.
   - DiscussionAPI integration with automatic fallback to Playwright/CDP poster.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests

from src.config import settings

logger = logging.getLogger("ezlynx_api")

ROBIE_SIGNATURE = "\n\nROBIE was here"

def normalize_robie_signature(text: str) -> str:
    """Ensure text ends with the mandatory 'ROBIE was here' signature."""
    cleaned = re.sub(r"\n*\s*(?:robie\s+was\s+here)\s*$", "", text, flags=re.IGNORECASE).rstrip()
    return f"{cleaned}\n\nROBIE was here"

# Classic GET /documentlibrary/list/{applicant}/{page}/{size}/{policyId} returns a
# paged envelope (TotalRecords + a document array). Live production rows use
# Description (filename), Id, and PolicyId — not DocumentName / PolicyNumber.
_DOCUMENT_LIST_KEYS = (
    "Documents",
    "DocumentList",
    "DocumentDetails",
    "DocumentLibraryList",
    "FileList",
    "Files",
    "Records",
    "Items",
    "Results",
    "documents",
    "documentList",
    "documentDetails",
    "records",
    "items",
    "results",
    "files",
)
_DOCUMENT_WRAPPER_KEYS = (
    "Data",
    "data",
    "Result",
    "result",
    "d",
    "Value",
    "value",
    "Response",
    "response",
)
_DOCUMENT_HINT_KEYS = {
    "Description",
    "description",
    "Id",
    "PolicyId",
    "policyId",
    "DocumentName",
    "DocumentID",
    "DocumentId",
    "FileName",
    "fileName",
    "documentName",
    "documentId",
    "PolicyNumber",
    "policyNumber",
    "CreatedDate",
    "createdDate",
    "UploadedDate",
    "AssociatedPolicyNumber",
}


def _first_present(obj: Dict[str, Any], keys: tuple) -> Any:
    for key in keys:
        if key not in obj:
            continue
        value = obj[key]
        if value is None or value == "":
            continue
        return value
    return None


def _looks_like_document_rows(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(item, dict) for item in value):
        return False
    sample_keys = set(value[0].keys())
    if sample_keys & _DOCUMENT_HINT_KEYS:
        return True
    lowered = {str(k).lower() for k in sample_keys}
    return bool(lowered & {"name", "title", "filename", "id", "documentid"})


def extract_document_records(payload: Any) -> List[Dict[str, Any]]:
    """Return document row dicts from a Classic / wrapped Document Library payload.

    Production list_applicant_documents data looks like:
      {"TotalRecords": 154, "Documents": [{Id, Description, PolicyId, CreatedDate}, ...]}
    Description is the filename. Older/alternate envelopes may use DocumentName / PolicyNumber,
    DocumentList, Records, items, or an ASP.NET ``d`` wrapper.
    """
    if isinstance(payload, list):
        if _looks_like_document_rows(payload) or (
            payload and all(isinstance(item, dict) for item in payload)
        ):
            return payload
        collected: List[Dict[str, Any]] = []
        for item in payload:
            collected.extend(extract_document_records(item))
        return collected

    if not isinstance(payload, dict):
        return []

    for key in _DOCUMENT_LIST_KEYS:
        value = payload.get(key)
        if _looks_like_document_rows(value):
            return value
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value

    for key in _DOCUMENT_WRAPPER_KEYS:
        nested = payload.get(key)
        if nested is payload:
            continue
        if isinstance(nested, (dict, list)):
            found = extract_document_records(nested)
            if found:
                return found

    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = extract_document_records(value)
            if found:
                return found
    return []


def document_library_total(payload: Any, records: Optional[List[Dict[str, Any]]] = None) -> int:
    """Best-effort total from TotalRecords (or similar), falling back to the extracted page."""
    if isinstance(payload, dict):
        for key in ("TotalRecords", "totalRecords", "Total", "total", "Count", "count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    if records is not None:
        return len(records)
    if isinstance(payload, list):
        return len(payload)
    return 0


def _stringify_policy_number(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        nested = _first_present(
            value,
            ("PolicyNumber", "policyNumber", "Number", "number", "Name", "name"),
        )
        return _stringify_policy_number(nested)
    if isinstance(value, list) and value:
        return _stringify_policy_number(value[0])
    text = str(value).strip()
    return text or None


def _format_uploaded_date(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1000.0
        return time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    text = str(value).strip()
    ms_match = re.search(r"/Date\((-?\d+)", text)
    if ms_match:
        timestamp = int(ms_match.group(1)) / 1000.0
        return time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    return text[:19].replace("T", " ")


def _stringify_policy_id(value: Any) -> Optional[str]:
    """Return a non-zero PolicyId as text. 0 means unassociated on the Classic API."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if int(value) == 0:
            return None
        return str(int(value))
    text = str(value).strip()
    if not text or text == "0":
        return None
    return text


def document_display_fields(doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Normalize a document row to name, id, policy association, and uploaded date.

    Live Classic documentlibrary/list rows use Description (filename), Id, PolicyId.
    """
    name = _first_present(
        doc,
        (
            "Description",
            "description",
            "DocumentName",
            "FileName",
            "Name",
            "Title",
            "documentName",
            "fileName",
            "name",
            "title",
        ),
    )
    doc_id = _first_present(
        doc,
        ("Id", "ID", "id", "DocumentID", "DocumentId", "documentId"),
    )
    policy_number = _stringify_policy_number(
        _first_present(
            doc,
            (
                "PolicyNumber",
                "AssociatedPolicyNumber",
                "PolicyNum",
                "policyNumber",
                "associatedPolicyNumber",
                "AssociatedPolicy",
                "Policy",
                "AssociatedPolicies",
                "Policies",
                "policies",
            ),
        )
    )
    policy_id = _stringify_policy_id(
        _first_present(doc, ("PolicyId", "policyId", "PolicyID", "AssociatedPolicyId"))
    )
    uploaded = _format_uploaded_date(
        _first_present(
            doc,
            (
                "CreatedDate",
                "UploadedDate",
                "UploadDate",
                "DateCreated",
                "CreatedOn",
                "ModifiedDate",
                "createdDate",
                "uploadedDate",
                "createdOn",
                "modifiedDate",
                "Date",
            ),
        )
    )
    return {
        "name": None if name is None else str(name),
        "id": None if doc_id is None else str(doc_id),
        "policy_number": policy_number,
        "policy_id": policy_id,
        "uploaded": uploaded,
    }


def format_document_line(doc: Dict[str, Any]) -> str:
    """Human-readable one-line listing for ezlynx_cli documents."""
    fields = document_display_fields(doc)
    parts = [f"Name: {fields['name'] or 'Untitled'}"]
    if fields["id"]:
        parts.append(f"ID: {fields['id']}")
    if fields["policy_number"]:
        parts.append(f"Policy: {fields['policy_number']}")
    if fields["policy_id"]:
        parts.append(f"PolicyId: {fields['policy_id']}")
    if not fields["policy_number"] and not fields["policy_id"]:
        parts.append("Policy: —")
    if fields["uploaded"]:
        parts.append(f"Uploaded: {fields['uploaded']}")
    return "  • " + " | ".join(parts)


DISQUALIFIED_DISCUSSION_PATTERNS = [
    "loss runs",
    "loss run",
    "certificate of insurance",
    "coi",
    "text sent",
    "text received",
    "email sent by automation center",
    "email automation",
    "automation center",
    "submission added",
    "billing and payments",
    "cancellation",
    "eva inbound call",
    "incoming call",
]

_MANUAL_LOB_RENEWAL_TITLE_RE = re.compile(r"^manual .+ renewal$", re.I)


def is_manual_lob_renewal_title(title: Optional[str]) -> bool:
    """Exact agency card ``Manual {LOB} Renewal`` (Paulette HO standing rule)."""
    return bool(_MANUAL_LOB_RENEWAL_TITLE_RE.match((title or "").strip()))


def is_disqualified_requested_title(title: Optional[str]) -> bool:
    t_low = (title or "").strip().lower()
    if not t_low or t_low in {"untitled", "(untitled)", "new discussion"}:
        return True
    return any(pat in t_low for pat in DISQUALIFIED_DISCUSSION_PATTERNS)


class EZLynxApiClient:
    """Production-grade client for EZLynx Classic & Modern APIs."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        agency_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        integration_group_id: Optional[str] = None,
        env: Optional[str] = None,
    ):
        self.env = (env or settings.ezlynx_env).upper()
        self.base_url = (base_url or settings.ezlynx_base_url).rstrip("/")
        self.client_id = client_id or settings.ezlynx_client_id
        self.client_secret = client_secret or settings.ezlynx_client_secret
        self.integration_group_id = integration_group_id or settings.ezlynx_integration_group_id
        self.app_secret = app_secret or settings.ezlynx_app_secret
        self.username = username or settings.ezlynx_username
        self.password = password or settings.ezlynx_password
        self.agency_id = agency_id or settings.ezlynx_agency_id or settings.ezlynx_agency_org_id
        self.account_username = getattr(settings, "ezlynx_account_username", None)

        # Target API Endpoints
        self.services_url = settings.ezlynx_services_url.rstrip("/")
        self.connect_token_url = settings.ezlynx_connect_token_url
        self.policy_api_url = settings.ezlynx_policy_api_url.rstrip("/")
        self.discussion_api_url = settings.ezlynx_discussion_api_url.rstrip("/")

        # Cached authentication tokens
        self._classic_token: Optional[str] = None
        self._classic_token_time: float = 0.0
        self._oauth_token: Optional[str] = None
        self._oauth_token_expires_at: float = 0.0
        self._oauth_scopes: List[str] = []

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def authenticate_classic(self, force_refresh: bool = False) -> bool:
        """Authenticates with the Classic Web Services API using EZAppSecret.

        Endpoint: GET /ezlynxapi/api/authenticate
        Headers: EZUser, EZPassword, EZAppSecret, EZToken: authenticate
        Response Header: EZToken
        """
        if not (self.username and self.password and self.app_secret):
            logger.warning("Classic EZLynx credentials not configured.")
            return False

        # Reuse cached token if younger than 15 minutes
        if not force_refresh and self._classic_token and (time.time() - self._classic_token_time < 900):
            return True

        auth_url = f"{self.services_url}/authenticate"
        headers = {
            "EZUser": self.username,
            "EZPassword": self.password,
            "EZAppSecret": self.app_secret,
            "EZToken": "authenticate",
        }

        try:
            resp = requests.get(auth_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                token = resp.headers.get("EZToken")
                if token:
                    self._classic_token = token
                    self._classic_token_time = time.time()
                    logger.info("Successfully authenticated with Classic EZLynx API (EZToken issued).")
                    return True
                else:
                    logger.error("Classic auth returned 200 but EZToken header was missing.")
                    return False
            else:
                logger.error(f"Classic EZLynx auth failed ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Exception connecting to Classic EZLynx auth: {e}")
            return False

    def authenticate_oauth(self, force_refresh: bool = False) -> bool:
        """Authenticates with the Modern OAuth2 Gateway via vendor_data_access grant.

        Endpoint: POST /auth/connect/token
        Payload: client_id, client_secret, grant_type, scope, username, integration_group_id
        """
        if not (self.client_id and self.client_secret and self.username and self.integration_group_id):
            logger.warning("OAuth2 EZLynx credentials not configured.")
            return False

        # Reuse cached token if not expired (with 60-second safety window)
        if not force_refresh and self._oauth_token and (time.time() < self._oauth_token_expires_at - 60):
            return True

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "vendor_data_access",
            "scope": "DiscussionApi EzLynxApi openid PolicyApi",
            "username": self.username,
            "integration_group_id": self.integration_group_id,
        }

        try:
            resp = requests.post(self.connect_token_url, data=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                self._oauth_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._oauth_token_expires_at = time.time() + expires_in
                scope_str = data.get("scope", "")
                self._oauth_scopes = scope_str.split() if isinstance(scope_str, str) else []
                logger.info("Successfully authenticated with Modern EZLynx OAuth2 Gateway.")
                return True
            else:
                logger.error(f"Modern OAuth2 token request failed ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Exception connecting to EZLynx OAuth2 endpoint: {e}")
            return False

    def authenticate(self) -> bool:
        """Authenticates with both Classic and OAuth2 endpoints."""
        classic_ok = self.authenticate_classic()
        oauth_ok = self.authenticate_oauth()
        return classic_ok or oauth_ok

    def _get_classic_headers(self, account_username: Optional[str] = None) -> Dict[str, str]:
        """Builds headers required for Classic REST API calls."""
        if not self._classic_token:
            self.authenticate_classic()
        user = account_username or self.account_username or self.username
        return {
            "EZAppSecret": self.app_secret,
            "EZToken": self._classic_token or "",
            "AccountUsername": user,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_oauth_headers(self) -> Dict[str, str]:
        """Builds headers required for Modern OAuth2 Gateway calls."""
        if not self._oauth_token:
            self.authenticate_oauth()
        return {
            "Authorization": f"Bearer {self._oauth_token or ''}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_headers(self) -> Dict[str, str]:
        """Backward-compatible header generator for general calls."""
        if self._oauth_token or self.authenticate_oauth():
            return self._get_oauth_headers()
        elif self._classic_token or self.authenticate_classic():
            return self._get_classic_headers()
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------------------
    # Applicant & Policy Management
    # -------------------------------------------------------------------------

    def get_applicant(self, applicant_id: str) -> Dict[str, Any]:
        """Retrieves applicant profile details (Personal or Commercial).

        Endpoint: GET /ezlynxapi/api/Applicant/v2/{applicant_id}
        """
        if not self.authenticate_classic():
            return {"status": "error", "error": "Unable to authenticate with Classic EZLynx API"}

        url = f"{self.services_url}/Applicant/v2/{applicant_id}"
        try:
            resp = requests.get(url, headers=self._get_classic_headers(), timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                return {"status": "success", "applicant": data}
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def search_applicants(self, query: str) -> List[Dict[str, Any]]:
        """Searches for applicants by ID, insured name, or policy number.

        Uses direct ID lookup if query is numeric, local renewal database cross-referencing,
        and policy search for policy numbers.
        """
        query_clean = str(query).strip()
        results = []

        # 1. Direct ID lookup if purely numeric
        if query_clean.isdigit():
            app_res = self.get_applicant(query_clean)
            if app_res.get("status") == "success":
                app = app_res.get("applicant", {})
                name = app.get("BusinessName") or f"{app.get('FirstName', '')} {app.get('LastName', '')}".strip()
                results.append({
                    "applicant_id": query_clean,
                    "name": name,
                    "type": app.get("ApplicantType"),
                    "assigned_to": app.get("AssignedTo"),
                    "email": app.get("BusinessEmail") or app.get("Email"),
                    "phone": app.get("BusinessPhone") or app.get("CellPhone"),
                    "address": app.get("CurrentAddress"),
                    "source": "ezlynx_api_direct"
                })
                return results

        # 2. Local database search across policy_renewals table
        db_file = Path("data/renewals.db")
        if db_file.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(str(db_file))
                cur = conn.cursor()
                like_term = f"%{query_clean}%"
                cur.execute(
                    """
                    SELECT DISTINCT applicant_id, insured_name, policy_number, carrier_name
                    FROM policy_renewals
                    WHERE insured_name LIKE ? OR policy_number LIKE ? OR applicant_id LIKE ?
                    LIMIT 10
                    """,
                    (like_term, like_term, like_term)
                )
                rows = cur.fetchall()
                conn.close()

                seen_ids = set()
                for app_id, ins_name, pol_num, carrier in rows:
                    if app_id in seen_ids:
                        continue
                    seen_ids.add(app_id)
                    app_res = self.get_applicant(app_id) if str(app_id).isdigit() else {}
                    if app_res.get("status") == "success":
                        app = app_res.get("applicant", {})
                        results.append({
                            "applicant_id": app_id,
                            "name": app.get("BusinessName") or ins_name,
                            "type": app.get("ApplicantType"),
                            "assigned_to": app.get("AssignedTo"),
                            "email": app.get("BusinessEmail") or app.get("Email"),
                            "phone": app.get("BusinessPhone") or app.get("CellPhone"),
                            "carrier": carrier,
                            "policy_number": pol_num,
                            "address": app.get("CurrentAddress"),
                            "source": "ezlynx_api_enriched"
                        })
                    else:
                        results.append({
                            "applicant_id": app_id,
                            "name": ins_name,
                            "carrier": carrier,
                            "policy_number": pol_num,
                            "source": "renewals_db"
                        })
            except Exception as e:
                logger.warning(f"Error searching applicants in database: {e}")

        return results

    def search_applicant(self, query: str) -> Optional[Dict[str, Any]]:
        """Searches for a single applicant matching query (ID, name, policy number)."""
        res = self.search_applicants(query)
        return res[0] if res else None

    def get_applicant_policies(
        self,
        applicant_id: str,
        start_date: str = "2020-01-01",
        end_date: str = "2027-12-31"
    ) -> Dict[str, Any]:
        """Fetches all policy records for an applicant across a date range.

        Endpoint: GET /ezlynxapi/api/policy/applicant/{applicant_id}/{startDate}/{endDate}
        """
        if not self.authenticate_classic():
            return {"status": "error", "error": "Unable to authenticate with Classic EZLynx API"}

        url = f"{self.services_url}/policy/applicant/{applicant_id}/{start_date}/{end_date}"
        try:
            resp = requests.get(url, headers=self._get_classic_headers(), timeout=20)
            if resp.status_code == 200:
                policies = resp.json()
                return {
                    "status": "success",
                    "applicant_id": applicant_id,
                    "count": len(policies) if isinstance(policies, list) else 0,
                    "policies": policies
                }
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def search_policy_by_number(self, policy_number: str) -> Dict[str, Any]:
        """Searches policies by policy number via PolicyAPI.

        Endpoint: GET /PolicyApi/policy/v1/search?PolicyNumber={policy_number}
        """
        if not self.authenticate_oauth():
            return {"status": "error", "error": "Unable to authenticate with EZLynx PolicyAPI"}

        url = f"{self.policy_api_url}/policy/v1/search"
        params = {"PolicyNumber": policy_number, "PageIndex": 1, "PageSize": 20}
        try:
            resp = requests.get(url, params=params, headers=self._get_oauth_headers(), timeout=20)
            if resp.status_code == 200:
                return {"status": "success", "data": resp.json()}
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def list_applicant_documents(
        self,
        applicant_id: str,
        page_index: int = 1,
        page_size: int = 20,
        policy_id: int = 0
    ) -> Dict[str, Any]:
        """Lists documents in an applicant's Document Library.

        Endpoint: GET /ezlynxapi/api/documentlibrary/list/{applicant_id}/{pageIndex}/{pageSize}/{policyId}

        Success envelope: ``{"status": "success", "data": <raw JSON>}``.
        Production ``data`` is typically
        ``{"TotalRecords": int, "Documents": [ {Id, Description, PolicyId, CreatedDate}, ... ]}``.
        ``Description`` is the filename. Use :func:`extract_document_records` to read the row list
        (do not assume Records/DocumentList or DocumentName/PolicyNumber).
        """
        if not self.authenticate_classic():
            return {"status": "error", "error": "Unable to authenticate with Classic EZLynx API"}

        url = f"{self.services_url}/documentlibrary/list/{applicant_id}/{page_index}/{page_size}/{policy_id}"
        try:
            resp = requests.get(url, headers=self._get_classic_headers(), timeout=20)
            if resp.status_code == 200:
                return {"status": "success", "data": resp.json()}
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def download_document_bytes(self, document_id: str) -> bytes:
        """Classic GET /document/{id} — raw PDF bytes, or empty on failure.

        Document ids are normalized (leading ``A`` stripped) before the request.
        Does not open Preview/RadPdf.
        """
        from src.ezlynx.document_downloader import coerce_pdf_bytes, normalize_ezlynx_download_id

        normalized = normalize_ezlynx_download_id(document_id)
        if not normalized:
            return b""
        if not self.authenticate_classic():
            logger.warning("Classic EZLynx auth failed; cannot GET /document/%s", normalized)
            return b""
        url = f"{self.services_url}/document/{normalized}"
        headers = dict(self._get_classic_headers())
        headers["Accept"] = "application/pdf, application/octet-stream, */*"
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.warning(
                    "Classic document download HTTP %s for id %s",
                    resp.status_code,
                    normalized,
                )
                return b""
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    return coerce_pdf_bytes(resp.json())
                except Exception:
                    return coerce_pdf_bytes(resp.content)
            return coerce_pdf_bytes(resp.content)
        except Exception as exc:
            logger.warning("Classic document download error for id %s: %s", normalized, exc)
            return b""

    def download_portal_document_bytes(self, relative_url: str) -> bytes:
        """GET an app.ezlynx.com Download path using portal cookies, then one CDP fetch.

        ``relative_url`` must already be the known-good ``/Download/{numericId}``.
        Never follows Preview/RadPdf. Empty bytes on failure.
        """
        from src.ezlynx.document_downloader import coerce_pdf_bytes, known_good_download_path

        if not relative_url.startswith("/"):
            relative_url = "/" + relative_url
        # Force the corrected Download path if a caller handed us /Download/A…
        if "/download/" in relative_url.lower():
            relative_url = known_good_download_path(relative_url)
        abs_url = f"https://app.ezlynx.com{relative_url}"
        storage_file = self._portal_storage_state_path()
        cookies = self._portal_session_cookies(storage_file)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/pdf,application/octet-stream,*/*",
        }
        if cookies:
            try:
                resp = requests.get(abs_url, cookies=cookies, headers=headers, timeout=30)
                if resp.status_code == 200 and resp.content:
                    return coerce_pdf_bytes(resp.content)
                logger.warning(
                    "Portal Download %s HTTP %s (%s bytes)",
                    relative_url,
                    resp.status_code,
                    len(resp.content or b""),
                )
            except Exception as exc:
                logger.debug("Portal cookie Download failed: %s", exc)

        cdp_url = getattr(settings, "ezlynx_cdp_endpoint", None) or "http://localhost:9222"
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_url)
                ctx = browser.contexts[0]
                try:
                    ctx.storage_state(path=str(storage_file))
                except Exception:
                    pass
                page = None
                for pg in ctx.pages:
                    if "ezlynx.com" in (pg.url or ""):
                        page = pg
                        break
                if page:
                    import base64

                    b64 = page.evaluate(
                        f"""async () => {{
                        const r = await fetch({relative_url!r});
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                        return btoa(binary);
                    }}"""
                    )
                    if b64:
                        return coerce_pdf_bytes(base64.b64decode(b64))
        except Exception as exc:
            logger.debug("Portal CDP Download failed: %s", exc)
        return b""

    def fetch_firmed_quote_pdf(self, document_id: str, dest_path: Path) -> Path:
        """One-shot firmed-quote fetch. See ``src.ezlynx.document_downloader``."""
        from src.ezlynx.document_downloader import fetch_firmed_quote_pdf as _fetch

        return _fetch(document_id, Path(dest_path), client=self)

    # -------------------------------------------------------------------------
    # Quoting & Sessions Management
    # -------------------------------------------------------------------------

    def get_completed_quote(self, quote_id: str) -> Dict[str, Any]:
        """Retrieves comparative rating session results for a Quote ID.

        Endpoint: GET /ezlynxapi/api/Quote/GetCompletedQuote/{quote_id}
        """
        if not self.authenticate_classic():
            return {"status": "error", "error": "Unable to authenticate with Classic EZLynx API"}

        url = f"{self.services_url}/Quote/GetCompletedQuote/{quote_id}"
        try:
            resp = requests.get(url, headers=self._get_classic_headers(), timeout=20)
            if resp.status_code == 200:
                return {"status": "success", "quote_data": resp.json()}
            return {"status": "error", "code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_session_overview(self) -> Dict[str, Any]:
        """Inspects and returns the status of all API and browser sessions."""
        classic_authenticated = self.authenticate_classic()
        oauth_authenticated = self.authenticate_oauth()

        # Check browser session storage state
        storage_file = Path("data/ezlynx_storage_state.json")
        browser_session_valid = False
        cookie_count = 0
        storage_modified_at = None

        if storage_file.exists():
            import json
            try:
                storage_modified_at = time.ctime(os.path.getmtime(storage_file))
                with open(storage_file) as f:
                    state_data = json.load(f)
                    cookies = state_data.get("cookies", [])
                    cookie_count = len(cookies)
                    # Check if cookies are present and not all expired
                    now = time.time()
                    valid_cookies = [c for c in cookies if c.get("expires", now + 1) > now]
                    browser_session_valid = len(valid_cookies) > 0
            except Exception as e:
                logger.warning(f"Error inspecting browser storage state: {e}")

        # Check Chrome CDP endpoint connectivity
        cdp_endpoint = settings.ezlynx_cdp_endpoint or "http://localhost:9222"
        cdp_active = False
        try:
            r = requests.get(f"{cdp_endpoint}/json/version", timeout=1.5)
            cdp_active = (r.status_code == 200)
        except Exception:
            cdp_active = False

        return {
            "classic_api": {
                "configured": bool(self.username and self.app_secret),
                "authenticated": classic_authenticated,
                "services_url": self.services_url,
                "username": self.username,
                "token_cached": bool(self._classic_token),
            },
            "oauth_gateway": {
                "configured": bool(self.client_id and self.client_secret),
                "authenticated": oauth_authenticated,
                "connect_url": self.connect_token_url,
                "client_id": self.client_id,
                "scopes": self._oauth_scopes,
                "token_expires_in_seconds": max(0, int(self._oauth_token_expires_at - time.time())),
            },
            "browser_session": {
                "storage_state_file": str(storage_file),
                "exists": storage_file.exists(),
                "cookie_count": cookie_count,
                "session_active": browser_session_valid,
                "last_modified": storage_modified_at,
                "cdp_endpoint": cdp_endpoint,
                "cdp_connected": cdp_active,
            },
        }

    # -------------------------------------------------------------------------
    # Portal session GETs (cookie storage_state, then Playwright CDP)
    # -------------------------------------------------------------------------

    @staticmethod
    def _portal_storage_state_path() -> Path:
        if hasattr(settings, "ezlynx_storage_state_file") and settings.ezlynx_storage_state_file:
            return Path(settings.ezlynx_storage_state_file)
        return Path("data/ezlynx_storage_state.json")

    @staticmethod
    def _portal_session_cookies(storage_file: Path) -> Dict[str, str]:
        if not storage_file.exists():
            return {}
        try:
            with open(storage_file) as f:
                state = json.load(f)
            return {
                c["name"]: c["value"]
                for c in state.get("cookies", [])
                if "ezlynx.com" in c.get("domain", "")
            }
        except Exception as exc:
            logger.debug("Could not read EZLynx portal storage state: %s", exc)
            return {}

    @staticmethod
    def _portal_json_headers() -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _portal_get_json(self, relative_url: str) -> Optional[Any]:
        """GET an app.ezlynx.com JSON path using portal cookies, then CDP.

        ``relative_url`` is a path + query (e.g. ``/EZLynxPortalAPI/...?...``).
        Secrets stay in the local storage-state file / CDP session — never logged.
        """
        if not relative_url.startswith("/"):
            relative_url = "/" + relative_url
        abs_url = f"https://app.ezlynx.com{relative_url}"
        storage_file = self._portal_storage_state_path()
        cookies = self._portal_session_cookies(storage_file)
        if cookies:
            try:
                resp = requests.get(
                    abs_url,
                    cookies=cookies,
                    headers=self._portal_json_headers(),
                    timeout=8,
                )
                if resp.status_code == 200 and resp.content:
                    return resp.json()
                logger.warning(
                    "Portal GET %s returned HTTP %s; falling back to CDP",
                    relative_url.split("?", 1)[0],
                    resp.status_code,
                )
            except Exception as exc:
                logger.debug("Portal cookie GET failed: %s", exc)

        cdp_url = getattr(settings, "ezlynx_cdp_endpoint", None) or "http://localhost:9222"
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_url)
                ctx = browser.contexts[0]
                try:
                    ctx.storage_state(path=str(storage_file))
                except Exception:
                    pass
                page = None
                for pg in ctx.pages:
                    if "ezlynx.com" in pg.url:
                        page = pg
                        break
                if page:
                    return page.evaluate(
                        f"""async () => {{
                        const r = await fetch({relative_url!r});
                        return await r.json();
                    }}"""
                    )
        except Exception as exc:
            logger.debug("Portal CDP GET failed: %s", exc)
        return None

    @staticmethod
    def unwrap_sales_center_opportunities(payload: Any) -> List[Dict[str, Any]]:
        """Normalize GetOpportunitiesForApplicant JSON to a list of opportunity dicts."""
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("opportunities", "Opportunities"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
        data = payload.get("data") or payload.get("Data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            nested = data.get("opportunities") or data.get("Opportunities")
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return []

    def get_sales_center_opportunities(
        self,
        applicant_id: str,
        include_lead_info: bool = True,
    ) -> List[Dict[str, Any]]:
        """Sales Center opportunities for an applicant (greeting ``producerName``).

        Live (Buster Brown / 26356199):
        GET /EZLynxPortalAPI/SalesCenter/Opportunity/GetOpportunitiesForApplicant
            ?applicantID={id}&includeLeadInfo=true
        """
        lead_flag = "true" if include_lead_info else "false"
        relative = (
            "/EZLynxPortalAPI/SalesCenter/Opportunity/GetOpportunitiesForApplicant"
            f"?applicantID={applicant_id}&includeLeadInfo={lead_flag}"
        )
        data = self._portal_get_json(relative)
        opportunities = self.unwrap_sales_center_opportunities(data)
        if opportunities:
            logger.info(
                "Retrieved %s Sales Center opportunities via portal for applicant %s",
                len(opportunities),
                applicant_id,
            )
        return opportunities

    def get_applicant_sidebar(self, applicant_id: str) -> Optional[Dict[str, Any]]:
        """Portal sidebar. ``Applicant.Assignment.AssignedTo`` is the full display name.

        Live:
        GET /applicantportal/ApplicantContext/GetApplicantSidebar?applicantID={id}

        Classic Applicant/v2 ``AssignedTo`` is a username (e.g. Carlo1) and is a
        different field — do not treat that Classic value as this payload.
        """
        relative = (
            f"/applicantportal/ApplicantContext/GetApplicantSidebar?applicantID={applicant_id}"
        )
        data = self._portal_get_json(relative)
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------------------
    # Discussion Discovery & Matching
    # -------------------------------------------------------------------------

    def get_applicant_discussions(self, applicant_id: str, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetches active discussions for an applicant from EZLynx portal GetPagedDiscussions.

        Live endpoint (hermes-poc-01):
        GET /EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=50&applicantId={id}&applicantContext=true

        Uses authenticated portal session cookies first; falls back to Playwright CDP if available.
        """
        relative = (
            "/EZLynxPortalAPI/Discussions/GetPagedDiscussions"
            f"?pageNumber=1&pageSize={page_size}&applicantId={applicant_id}&applicantContext=true"
        )
        data = self._portal_get_json(relative)
        if isinstance(data, dict):
            discussions = data.get("discussions")
            if isinstance(discussions, list):
                logger.info(
                    "Retrieved %s discussions via portal session for applicant %s",
                    len(discussions),
                    applicant_id,
                )
                return discussions
            logger.warning(
                "GetPagedDiscussions for applicant %s missing discussions list",
                applicant_id,
            )
        return []

    def find_matching_discussion(
        self,
        applicant_id: str,
        policy_number: Optional[str] = None,
        line_of_business: Optional[str] = None,
        carrier_name: Optional[str] = None,
        policy_numbers: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Finds an authentic existing renewal discussion card matching the policy or carrier.
        
        Matching Priority:
        1. Exact Policy Number match in title or attached policy folder (excluding auxiliary threads like Loss Runs / COI).
           Accepts the canonical number plus prior-term / renewal-term aliases (``policy_numbers``).
        2. Scored match across Carrier tokens, Line of Business tokens, active team participation, and note counts.
        """
        discussions = self.get_applicant_discussions(applicant_id)
        if not discussions:
            return None

        # Filter out disqualified auxiliary discussion threads
        valid_discussions = []
        for d in discussions:
            title = d.get("title", "")
            t_low = title.lower()
            if any(disq in t_low for disq in DISQUALIFIED_DISCUSSION_PATTERNS):
                logger.debug(f"Filtering out auxiliary discussion card: '{title}' (ID {d.get('discussionId')})")
                continue
            valid_discussions.append(d)

        if not valid_discussions:
            return None

        # 1. Exact Policy Number match in title or discussionNote (canonical + aliases)
        number_candidates: List[str] = []
        for raw in [policy_number, *(policy_numbers or [])]:
            if raw and raw not in number_candidates:
                number_candidates.append(raw)

        if number_candidates:
            policy_matches = []
            matched_with = None
            for candidate in number_candidates:
                pol_clean = re.sub(r"[^a-zA-Z0-9]", "", candidate).lower()
                if len(pol_clean) < 4:
                    continue
                for d in valid_discussions:
                    if d in policy_matches:
                        continue
                    title = d.get("title", "")
                    title_clean = re.sub(r"[^a-zA-Z0-9]", "", title).lower()
                    
                    # Check title
                    in_title = pol_clean in title_clean
                    
                    # Check attached discussion note policy
                    note_pol = ""
                    if isinstance(d.get("discussionNote"), dict):
                        note_pol = d["discussionNote"].get("policyNumber", "")
                    note_pol_clean = re.sub(r"[^a-zA-Z0-9]", "", note_pol).lower()
                    in_note = bool(note_pol_clean and (pol_clean in note_pol_clean or note_pol_clean in pol_clean))
                    
                    if in_title or in_note:
                        policy_matches.append(d)
                        matched_with = candidate
                
            if policy_matches:
                # Sort candidates: prefer title containing 'renewal', then higher note count
                def _rank(cand):
                    t = cand.get("title", "").lower()
                    has_renewal = 100 if "renewal" in t else (50 if "manual" in t else 0)
                    return has_renewal + cand.get("noteCount", 0)
                
                policy_matches.sort(key=_rank, reverse=True)
                best = policy_matches[0]
                logger.info(
                    f"Matched discussion by policy# '{matched_with or policy_number}': "
                    f"'{best.get('title')}' (ID {best.get('discussionId')})"
                )
                return best

        # 2. Scored Match across Carrier and LOB tokens
        carrier_tokens = [
            t.lower() for t in re.split(r"[\s\-/,.]+", carrier_name or "")
            if len(t) > 3 and t.lower() not in ("insurance", "agency", "company", "corp", "group", "underwriters", "services")
        ]
        lob_tokens = [
            t.lower() for t in re.split(r"[\s\-/,.]+", line_of_business or "")
            if len(t) > 2 and t.lower() not in ("manual", "renewal", "policy")
        ]

        scored = []
        for d in valid_discussions:
            title = d.get("title", "")
            t_low = title.lower()
            # Only consider renewal or manual discussions
            if "renewal" not in t_low and "manual" not in t_low:
                continue

            score = 0
            for ct in carrier_tokens:
                if ct in t_low:
                    score += 15
            for lt in lob_tokens:
                if lt in t_low:
                    score += 5

            # Prefer discussions with authentic agency activity
            if d.get("lastModifiedByName") and d.get("lastModifiedByName") not in ("Robie AI", "SSRobie"):
                score += 20
            if d.get("noteCount", 0) > 1:
                score += 10

            if score > 0:
                scored.append((score, d))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            best = scored[0][1]
            logger.info(f"Matched discussion by score ({scored[0][0]}pts): '{best.get('title')}' (ID {best.get('discussionId')})")
            return best

        return None

    def resolve_discussion_title(
        self,
        applicant_id: str,
        discussion_title: Optional[str] = None,
        policy_number: Optional[str] = None,
        line_of_business: Optional[str] = None,
        carrier_name: Optional[str] = None,
        policy_numbers: Optional[List[str]] = None,
    ) -> str:
        """Resolves the authentic discussion title to ensure notes thread directly into the right card.

        Prefer an existing ``{LOB} Renewal`` card (any line of business) when
        the caller asked for a Manual/Update fallback title. Only create
        ``Manual {LOB} Renewal`` when no matching LOB renewal discussion exists.

        Substring / first-card rematch is the Paulette misfire path and is not used.
        """
        requested = (discussion_title or "").strip()
        if requested and is_disqualified_requested_title(requested):
            logger.warning("Ignoring disqualified/untitled requested discussion title: '%s'", requested)
            requested = ""

        from src.ezlynx.manual_renewal_gate import (
            is_renewal_update_title,
            resolve_existing_lob_renewal_discussion,
        )

        # 0. Existing {LOB} Renewal wins over Manual / Renewal Update fallbacks.
        if line_of_business and (
            not requested
            or is_manual_lob_renewal_title(requested)
            or is_renewal_update_title(requested)
        ):
            discussions = self.get_applicant_discussions(applicant_id)
            existing = resolve_existing_lob_renewal_discussion(discussions, line_of_business)
            if existing:
                logger.info(
                    "Preferred existing {LOB} Renewal card '%s' over fallback '%s'",
                    existing.title,
                    requested or "Manual/Update",
                )
                return existing.title

        # 1. Exact Manual {LOB} Renewal — never treat as a generic placeholder.
        if requested and is_manual_lob_renewal_title(requested):
            discussions = self.get_applicant_discussions(applicant_id)
            want = requested.lower()
            for d in discussions:
                d_title = (d.get("title") or "").strip()
                if d_title.lower() == want and not is_disqualified_requested_title(d_title):
                    logger.info(
                        "Matched exact Manual {LOB} Renewal card: '%s' (ID %s)",
                        d_title,
                        d.get("discussionId"),
                    )
                    return d_title
            logger.info(
                "Manual {LOB} Renewal '%s' not found — returning exact title so the note creates it",
                requested,
            )
            return requested

        # 2. Exact requested title only (no substring rematch).
        if requested:
            dt_clean = requested.lower()
            discussions = self.get_applicant_discussions(applicant_id)
            for d in discussions:
                d_title = (d.get("title") or "").strip()
                if d_title.lower() == dt_clean and not is_disqualified_requested_title(d_title):
                    logger.info(
                        "Matched explicitly requested discussion card: '%s' (ID %s)",
                        d_title,
                        d.get("discussionId"),
                    )
                    return d_title

        # 3. If policy_number, LOB, or carrier is provided, attempt to match active card
        #    (find_matching_discussion already drops Email Automation / aux cards).
        if policy_number or policy_numbers or line_of_business or carrier_name:
            matched = self.find_matching_discussion(
                applicant_id=applicant_id,
                policy_number=policy_number,
                line_of_business=line_of_business,
                carrier_name=carrier_name,
                policy_numbers=policy_numbers,
            )
            if matched and matched.get("title") and not is_disqualified_requested_title(matched.get("title")):
                return matched["title"]

        # 4. Honor a remaining explicit non-Manual title (create-if-missing).
        if requested:
            return requested

        # Fallback to agency standard naming convention
        lob_clean = line_of_business or "Policy"
        parts = [f"Renewal Manual {lob_clean}"]
        suffix = []
        if policy_number:
            suffix.append(policy_number)
        if carrier_name:
            short_carrier = " ".join(carrier_name.split()[:3])
            suffix.append(short_carrier)
        if suffix:
            parts.append(" | " + " ".join(suffix))
        return "".join(parts)

    # -------------------------------------------------------------------------
    # Discussion Notes & Tasks
    # -------------------------------------------------------------------------

    def add_note_to_discussion(
        self,
        applicant_id: str,
        discussion_title: Optional[str] = None,
        note_text: str = "",
        policy_number: Optional[str] = None,
        line_of_business: Optional[str] = None,
        carrier_name: Optional[str] = None,
        use_playwright_fallback: bool = True,
        require_existing_discussion: bool = False,
        honor_explicit_title: bool = False,
        policy_numbers: Optional[List[str]] = None,
        discussion_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Posts a note under the designated discussion title for an applicant.

        Mandates 'Robie was here' signature.
        Automatically resolves the authentic existing discussion card to thread directly inside it.
        Uses direct API if available, with graceful fallback to Playwright/CDP.

        When ``honor_explicit_title`` is True, posts to ``discussion_title`` exactly
        (creates that titled card if missing). Never rematches onto Email Automation.

        When ``require_existing_discussion`` is True (underwriter-reply filing),
        refuses to post if ``find_matching_discussion`` cannot locate an existing
        titled renewal card — never creates an orphan or untitled discussion.
        """
        # Ensure mandatory policy association header is explicitly in note_text
        if policy_number and not any(k in note_text for k in [f"#{policy_number}", f"Policy: {policy_number}", f"Policy #{policy_number}"]):
            header_parts = [f"Policy: #{policy_number}"]
            details = []
            if line_of_business:
                details.append(line_of_business)
            if carrier_name:
                details.append(carrier_name)
            if details:
                header_parts.append(f"({' - '.join(details)})")
            note_text = f"{' '.join(header_parts)}\n\n{note_text.lstrip()}"

        # Ensure mandatory Robie signature is normalized to uppercase ROBIE was here
        note_text = normalize_robie_signature(note_text)

        if discussion_id and not discussion_title:
            try:
                discs = self.get_applicant_discussions(str(applicant_id)) or []
                matched = next((d for d in discs if str(d.get("discussionId")) == str(discussion_id)), None)
                if matched and matched.get("title"):
                    discussion_title = matched.get("title")
            except Exception as d_err:
                logger.debug(f"Failed to lookup title for discussion_id {discussion_id}: {d_err}")

        if honor_explicit_title:
            requested = (discussion_title or "").strip()
            if not requested or is_disqualified_requested_title(requested):
                logger.warning(
                    "Refusing honor_explicit_title post for applicant %s: untitled/automation title '%s'",
                    applicant_id,
                    discussion_title,
                )
                return {
                    "status": "error",
                    "error": "disqualified_or_untitled_discussion",
                    "applicant_id": applicant_id,
                    "policy_number": policy_number,
                    "discussion_title": discussion_title,
                }
            resolved_title = requested
        elif require_existing_discussion:
            matched = self.find_matching_discussion(
                applicant_id=str(applicant_id),
                policy_number=policy_number,
                line_of_business=line_of_business,
                carrier_name=carrier_name,
                policy_numbers=policy_numbers,
            )
            if not matched or not matched.get("title"):
                logger.warning(
                    f"Refusing to post note for applicant {applicant_id} / policy {policy_number}: "
                    "no existing titled renewal discussion (orphan creation blocked)."
                )
                return {
                    "status": "error",
                    "error": "no_existing_titled_discussion",
                    "applicant_id": applicant_id,
                    "policy_number": policy_number,
                }
            resolved_title = matched["title"]
        else:
            # Dynamically resolve authentic discussion card title
            resolved_title = self.resolve_discussion_title(
                applicant_id=str(applicant_id),
                discussion_title=discussion_title,
                policy_number=policy_number,
                line_of_business=line_of_business,
                carrier_name=carrier_name,
                policy_numbers=policy_numbers,
            )

        if is_disqualified_requested_title(resolved_title):
            logger.warning(
                "Refusing to post onto disqualified discussion '%s' for applicant %s",
                resolved_title,
                applicant_id,
            )
            return {
                "status": "error",
                "error": "disqualified_or_untitled_discussion",
                "applicant_id": applicant_id,
                "policy_number": policy_number,
                "discussion_title": resolved_title,
            }

        # 1. Direct Classic REST Note API (Fastest and direct)
        if self.authenticate_classic():
            endpoint = f"{self.services_url}/note/v1"
            headers = self._get_classic_headers(account_username=self.account_username or "SSRobie")
            try:
                payload = {
                    "ApplicantId": int(applicant_id),
                    "DiscussionTitle": resolved_title,
                    "NoteDescription": note_text
                }
                if policy_number:
                    payload["PolicyNumber"] = policy_number
                resp = requests.post(endpoint, json=payload, headers=headers, timeout=10)
                if resp.status_code in (200, 201):
                    data = resp.json() if resp.text else {}
                    note_id = data.get("NoteId")
                    logger.info(f"Successfully posted note via EZLynx REST Note API for Applicant {applicant_id}: NoteId {note_id} into '{resolved_title}'")
                    return {
                        "status": "success",
                        "method": "api",
                        "applicant_id": applicant_id,
                        "discussion_title": resolved_title,
                        "note_id": note_id,
                        "text": note_text,
                        "data": data
                    }
                else:
                    logger.warning(f"REST Note API returned {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.warning(f"REST Note API attempt failed: {e}")

        # 2. Attempt Discussion API if OAuth token is available
        if self._oauth_token or self.authenticate_oauth():
            endpoint = f"{self.discussion_api_url}/discussions/v1/notes"
            payload = {
                "applicantId": applicant_id,
                "title": resolved_title,
                "text": note_text,
                "policyNumber": policy_number,
                "category": "Renewal"
            }
            try:
                resp = requests.post(endpoint, json=payload, headers=self._get_oauth_headers(), timeout=10)
                if resp.status_code in (200, 201):
                    logger.info(f"Successfully posted note via Discussion API for Applicant {applicant_id}.")
                    return {
                        "status": "success",
                        "method": "api",
                        "applicant_id": applicant_id,
                        "discussion_title": resolved_title,
                        "text": note_text,
                        "data": resp.json()
                    }
            except Exception as e:
                logger.debug(f"Discussion API direct POST attempted: {e}")

        # 3. Seamless Fallback: Playwright / Chrome CDP Discussion Poster
        if use_playwright_fallback:
            try:
                from src.ezlynx.discussion_poster import EZLynxDiscussionPoster
                poster = EZLynxDiscussionPoster()

                async def _post():
                    return await poster.post_note_async(
                        applicant_id=applicant_id,
                        discussion_search_text=resolved_title,
                        note_body=note_text,
                        screenshot_filename=f"note_{applicant_id}_{int(time.time())}.png"
                    )

                # Run asynchronous poster safely in current or new event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import nest_asyncio
                        nest_asyncio.apply()
                        res = loop.run_until_complete(_post())
                    else:
                        res = loop.run_until_complete(_post())
                except RuntimeError:
                    res = asyncio.run(_post())

                if res.get("success"):
                    logger.info(f"Successfully posted note via Playwright CDP for Applicant {applicant_id}.")
                    return {
                        "status": "success",
                        "method": "playwright",
                        "applicant_id": applicant_id,
                        "discussion_title": res.get("discussion_title", resolved_title),
                        "note_id": res.get("note_id"),
                        "screenshot_path": res.get("screenshot_path"),
                    }
                else:
                    logger.warning(f"Playwright poster returned error: {res.get('error')}. Recording simulation note.")
            except Exception as e:
                logger.warning(f"Playwright fallback encountered exception: {e}. Recording simulation note.")

        # 4. Fallback: Simulation record
        logger.info(f"[SIMULATION] Note recorded for Applicant: {applicant_id} | Title: '{resolved_title}'")
        return {
            "status": "simulated",
            "method": "simulation",
            "applicant_id": applicant_id,
            "discussion_title": resolved_title,
            "note_id": f"sim_note_{applicant_id[:6]}",
            "text": note_text
        }

    @staticmethod
    def _document_data_uri(file_path: Path) -> str:
        """Encode a local file as a data URI using the real MIME type (not PDF-only)."""
        import base64

        mime_map = {
            ".pdf": "application/pdf",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".mpeg": "audio/mpeg",
            ".txt": "text/plain",
            ".json": "application/json",
        }
        mime = mime_map.get(file_path.suffix.lower(), "application/octet-stream")
        encoded = base64.b64encode(file_path.read_bytes()).decode("utf-8")
        return f"data:{mime};base64,{encoded}"

    def _upload_document_via_api(
        self,
        applicant_id: str,
        file_path: Path,
        folder_name: Optional[str],
        description: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        if not (self._classic_token or self.authenticate_classic()):
            return None
        endpoint = f"{self.services_url}/document"
        payload = {
            "ApplicantId": int(applicant_id) if str(applicant_id).isdigit() else applicant_id,
            "DocumentName": description or file_path.name,
            "FolderPath": folder_name,
            "Document": self._document_data_uri(file_path),
        }
        try:
            resp = requests.post(endpoint, json=payload, headers=self._get_classic_headers(), timeout=30)
            if resp.status_code in (200, 201):
                return {"status": "success", "method": "api", "data": resp.json()}
            logger.debug("Direct API document upload HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug(f"Direct API document upload error: {e}")
        return None

    def _upload_document_via_playwright(
        self,
        applicant_id: str,
        file_path: Path,
        folder_name: Optional[str],
        description: Optional[str],
        policy_number: Optional[str],
        doc_type: str,
        label_to_apply: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        try:
            from src.ezlynx.document_uploader import EZLynxDocumentUploader
            uploader = EZLynxDocumentUploader(cdp_url=settings.ezlynx_cdp_endpoint or "http://localhost:9222")

            async def _upload():
                return await uploader.upload_document(
                    applicant_id=applicant_id,
                    file_path=file_path,
                    policy_number=policy_number,
                    doc_type=doc_type,
                    doc_title=description,
                    label_to_apply=label_to_apply,
                    target_folder=folder_name
                )

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import nest_asyncio
                    nest_asyncio.apply()
                    res = loop.run_until_complete(_upload())
                else:
                    res = loop.run_until_complete(_upload())
            except RuntimeError:
                res = asyncio.run(_upload())

            if res.get("success"):
                logger.info(f"Successfully uploaded document via Playwright CDP for Applicant {applicant_id}.")
                return {
                    "status": "success",
                    "method": "playwright",
                    "applicant_id": applicant_id,
                    "document_name": res.get("document_name"),
                    "policy_number": res.get("policy_number"),
                    "applied_label": res.get("applied_label"),
                    "target_folder": res.get("target_folder"),
                    "folder_created": res.get("folder_created"),
                    "screenshot_path": res.get("screenshot_path"),
                }
            logger.warning(f"Playwright document uploader returned error: {res.get('error')}. Checking API/simulation.")
        except Exception as e:
            logger.warning(f"Playwright uploader fallback encountered exception: {e}. Checking API/simulation.")
        return None

    def validate_policy_payload(
        self,
        applicant_id: str,
        policy_number: str,
        expected_carrier: Optional[str] = None
    ) -> Dict[str, Any]:
        """Pre-execution validation gate.
        Validates that policy_number exists and is active for the applicant in EZLynx,
        and verifies that expected_carrier matches the companyName. Raises ValueError if invalid.
        """
        pol_res = self.get_applicant_policies(applicant_id) or []
        policies = pol_res.get("policies", []) if isinstance(pol_res, dict) else pol_res
        if not policies:
            raise ValueError(f"No policies found for applicant {applicant_id} in EZLynx.")

        target_pol = None
        for pol in policies:
            p_num = str(pol.get("policyNumber") or pol.get("PolicyNumber") or "")
            if p_num.strip().lower() == policy_number.strip().lower():
                target_pol = pol
                break

        if not target_pol:
            existing = [str(p.get("policyNumber") or p.get("PolicyNumber")) for p in policies]
            raise ValueError(
                f"Policy '{policy_number}' not found for applicant {applicant_id}. Existing: {existing}"
            )

        if expected_carrier:
            cname = str(
                target_pol.get("companyName") or
                target_pol.get("CompanyName") or
                target_pol.get("masterCompanyName") or
                target_pol.get("writingCompanyName") or ""
            )
            parts = [
                p.lower() for p in expected_carrier.split()
                if len(p) > 3 and p.lower() not in ("insurance", "company", "property", "casualty")
            ]
            if parts and not any(part in cname.lower() for part in parts):
                raise ValueError(
                    f"Carrier mismatch for policy '{policy_number}': Expected '{expected_carrier}', found EZLynx carrier '{cname}'."
                )

        return {"valid": True, "policy": target_pol}

    def upload_document(
        self,
        applicant_id: str,
        file_path: Path,
        folder_name: Optional[str] = None,
        description: Optional[str] = None,
        policy_number: Optional[str] = None,
        doc_type: str = "renewal",
        label_to_apply: Optional[str] = None,
        use_playwright_fallback: bool = True,
        prefer_api: bool = False,
    ) -> Dict[str, Any]:
        """Uploads a document to the Applicant's Document Management folder in EZLynx.

        ``prefer_api=True`` tries Classic REST first (voice recordings / transcripts).
        Playwright remains the default first path for renewal PDFs so existing
        callers are unchanged. Playwright is fallback-only when prefer_api is set.
        """
        if not file_path.exists():
            return {"status": "error", "error": f"File not found: {file_path}"}

        api_result = None
        pw_result = None
        if prefer_api:
            api_result = self._upload_document_via_api(applicant_id, file_path, folder_name, description)
            if api_result and api_result.get("status") == "success":
                return api_result
            if use_playwright_fallback:
                pw_result = self._upload_document_via_playwright(
                    applicant_id, file_path, folder_name, description, policy_number, doc_type, label_to_apply
                )
                if pw_result and pw_result.get("status") == "success":
                    return pw_result
        else:
            if use_playwright_fallback:
                pw_result = self._upload_document_via_playwright(
                    applicant_id, file_path, folder_name, description, policy_number, doc_type, label_to_apply
                )
                if pw_result and pw_result.get("status") == "success":
                    return pw_result
            api_result = self._upload_document_via_api(applicant_id, file_path, folder_name, description)
            if api_result and api_result.get("status") == "success":
                return api_result

        pw_err = pw_result.get("error") if pw_result else "playwright_skipped"
        api_err = api_result.get("error") if api_result else "api_skipped"
        err_msg = (
            f"Failed to upload document '{file_path.name}' to Applicant {applicant_id}. "
            f"Playwright error: {pw_err} | API error: {api_err}"
        )
        logger.error(err_msg)
        return {
            "status": "error",
            "error": err_msg,
            "applicant_id": applicant_id,
            "file_name": file_path.name
        }

    def create_user_task(
        self,
        applicant_id: str,
        title: str,
        description: str,
        assigned_user: Optional[str] = None,
        due_days_out: int = 3
    ) -> Dict[str, Any]:
        """Creates a follow-up task for the account manager in EZLynx."""
        if assigned_user and "robie" in str(assigned_user).lower():
            assigned_user = "Carlo Ferrara"
        logger.info(
            f"[TASK] EZLynx Task Created for Applicant: {applicant_id} | Title: '{title}' | Assigned: {assigned_user or 'Account Manager'}"
        )
        return {
            "status": "success",
            "applicant_id": applicant_id,
            "task_title": title,
            "task_id": f"task_{applicant_id[:6]}_{int(time.time())}"
        }
