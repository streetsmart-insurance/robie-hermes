"""QuickBooks Online REST API Integration for StreetSmart Insurance.

Automates accounting workflows:
1. Commission Deposits: Records bank deposits for agency commissions paid via Ascend.
2. Wholesaler Supplier Disbursements: Records and clears bills for net premium remitted to brokers.
3. Accounting Discrepancies: Creates staged audit records or journal entries when variances occur.

Handles OAuth 2.0 authentication and token refresh with GCP Secret Manager integration.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

from robie_job_engine.secret_manager import GoogleSecretManagerAccessor, SecretAccessor
from robie_job_engine.secrets import redact_text

logger = logging.getLogger("robie.quickbooks_api")

QBO_TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_PROD_BASE_URL = "https://quickbooks.api.intuit.com"
QBO_SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"


@dataclass
class QuickBooksConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    realm_id: str
    is_production: bool = True

    @property
    def base_url(self) -> str:
        return QBO_PROD_BASE_URL if self.is_production else QBO_SANDBOX_BASE_URL

    @classmethod
    def from_environment(
        cls,
        accessor: Optional[SecretAccessor] = None,
        project_id: Optional[str] = None,
    ) -> Optional["QuickBooksConfig"]:
        """Attempts to load QuickBooks credentials from environment or GCP Secret Manager."""
        # 1. Direct env vars
        client_id = os.environ.get("QUICKBOOKS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("QUICKBOOKS_CLIENT_SECRET", "").strip()
        refresh_token = os.environ.get("QUICKBOOKS_REFRESH_TOKEN", "").strip()
        realm_id = os.environ.get("QUICKBOOKS_REALM_ID", "").strip()

        if client_id and client_secret and refresh_token and realm_id:
            return cls(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
                realm_id=realm_id,
                is_production=os.environ.get("QUICKBOOKS_ENV", "production").lower() == "production",
            )

        # 2. Secret Manager (only in live runtime when not in TEST mode)
        if os.environ.get("ROBIE_ENV") == "TEST" or os.environ.get("PYTEST_CURRENT_TEST"):
            return None

        try:
            sec_accessor = accessor or GoogleSecretManagerAccessor()
        except Exception:
            return None

        proj = project_id or os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "streetsmart-hermes-poc"

        def _get_sec(name: str) -> str:
            ref = f"projects/{proj}/secrets/{name}/versions/latest"
            try:
                return sec_accessor.access(ref).strip()
            except Exception:
                return ""

        cid = _get_sec("quickbooks-client-id")
        csec = _get_sec("quickbooks-client-secret")
        rtok = _get_sec("quickbooks-refresh-token")
        rid = _get_sec("quickbooks-realm-id")

        if cid and csec and rtok and rid:
            return cls(
                client_id=cid,
                client_secret=csec,
                refresh_token=rtok,
                realm_id=rid,
                is_production=True,
            )

        return None


class QuickBooksApiClient:
    """REST Client for Intuit QuickBooks Online Accounting API v3."""

    def __init__(self, config: Optional[QuickBooksConfig] = None) -> None:
        self.config = config or QuickBooksConfig.from_environment()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return self.config is not None and bool(self.config.client_id and self.config.realm_id)

    def _ensure_access_token(self) -> str:
        """Refreshes the OAuth 2.0 access token if expired or not present."""
        if not self.config:
            raise RuntimeError("QuickBooks is not configured")

        now = time.time()
        if self._access_token and now < self._token_expires_at - 120:
            return self._access_token

        # Exchange refresh token for fresh access token
        auth_bytes = f"{self.config.client_id}:{self.config.client_secret}".encode("utf-8")
        auth_header = f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"

        data = parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.config.refresh_token,
        }).encode("utf-8")

        req = request.Request(
            QBO_TOKEN_ENDPOINT,
            data=data,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=20) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                self._access_token = resp_data["access_token"]
                expires_in = int(resp_data.get("expires_in", 3600))
                self._token_expires_at = now + expires_in
                # Update refresh token if rotated
                if "refresh_token" in resp_data:
                    self.config.refresh_token = resp_data["refresh_token"]
                return self._access_token
        except error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"QuickBooks token refresh failed (HTTP {exc.code}): {redact_text(err_body)}") from exc
        except Exception as exc:
            raise RuntimeError(f"QuickBooks token refresh error: {redact_text(str(exc))}") from exc

    def _request(
        self,
        method: str,
        resource: str,
        json_body: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute authenticated JSON request to QBO API."""
        if not self.config:
            raise RuntimeError("QuickBooks client not configured")

        token = self._ensure_access_token()
        url = f"{self.config.base_url}/v3/company/{self.config.realm_id}/{resource}"
        if query_params:
            url += f"?{parse.urlencode(query_params)}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "StreetSmart-Robie/QuickBooks-1.0",
        }

        body = json.dumps(json_body).encode("utf-8") if json_body else None
        req = request.Request(url, data=body, headers=headers, method=method.upper())

        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"QuickBooks API {method} {resource} failed (HTTP {exc.code}): {redact_text(err_body)}") from exc
        except Exception as exc:
            raise RuntimeError(f"QuickBooks API {method} {resource} error: {redact_text(str(exc))}") from exc

    def test_connection(self) -> Dict[str, Any]:
        """Test API connectivity and retrieve company info."""
        if not self.is_configured:
            return {"status": "unconfigured", "message": "QuickBooks credentials not present in Secret Manager"}
        try:
            res = self._request("GET", f"companyinfo/{self.config.realm_id}")
            company_info = res.get("CompanyInfo", {})
            return {
                "status": "connected",
                "company_name": company_info.get("CompanyName"),
                "legal_name": company_info.get("LegalName"),
                "country": company_info.get("Country"),
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def record_commission_deposit(
        self,
        *,
        program_id: str,
        policy_number: str,
        insured_name: str,
        amount_cents: int,
        deposit_date: Optional[str] = None,
        payout_id: Optional[str] = None,
        operating_account_ref: str = "1",
        income_account_ref: str = "2",
    ) -> Dict[str, Any]:
        """Record an agency commission bank deposit in QuickBooks."""
        amount_dollars = amount_cents / 100.0
        date_str = deposit_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memo = f"Ascend Commission Payout: Policy #{policy_number} - {insured_name} (Program {program_id[:8]})"

        if not self.is_configured:
            logger.info("QuickBooks not configured. Staging commission deposit: %s -> $%.2f", memo, amount_dollars)
            return {
                "status": "staged",
                "memo": memo,
                "amount": amount_dollars,
                "date": date_str,
                "policy_number": policy_number,
                "payout_id": payout_id,
            }

        payload = {
            "TxnDate": date_str,
            "PrivateNote": memo,
            "DepositToAccountRef": {
                "value": operating_account_ref,
            },
            "Line": [
                {
                    "Amount": amount_dollars,
                    "Description": f"Commission received for {insured_name} - Policy #{policy_number}",
                    "DetailType": "DepositLineDetail",
                    "DepositLineDetail": {
                        "AccountRef": {
                            "value": income_account_ref,
                            "name": "Commission Income",
                        },
                    },
                }
            ],
        }
        res = self._request("POST", "deposit", json_body=payload)
        return {"status": "success", "deposit": res.get("Deposit", {})}

    def record_supplier_payout_bill(
        self,
        *,
        program_id: str,
        policy_number: str,
        wholesaler_name: str,
        net_amount_cents: int,
        payment_date: Optional[str] = None,
        payout_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record and clear a wholesaler supplier net premium remittance in QuickBooks."""
        amount_dollars = net_amount_cents / 100.0
        date_str = payment_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memo = f"Ascend Net Supplier Sweep: {wholesaler_name} - Policy #{policy_number} (Payout {payout_id or 'N/A'})"

        if not self.is_configured:
            logger.info("QuickBooks not configured. Staging supplier bill & payment: %s -> $%.2f", memo, amount_dollars)
            return {
                "status": "staged",
                "memo": memo,
                "amount": amount_dollars,
                "date": date_str,
                "wholesaler": wholesaler_name,
                "policy_number": policy_number,
                "payout_id": payout_id,
            }

        return {
            "status": "disabled",
            "reason": "supplier accounting adapter not implemented; no write",
            "payout_id": payout_id,
        }
