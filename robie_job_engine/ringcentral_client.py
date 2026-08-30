"""RingCentral API Client for call logs and analytics.

Supports:
1. JWT OAuth2 Authentication (`/restapi/oauth/token` with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`).
2. Server-to-Server OAuth.
3. Fetching detailed Account Call Logs (`/restapi/v1.0/account/~/call-log`).
4. Mapping RingCentral extensions to employee names.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from robie_job_engine.productivity import RingCentralCall


class RingCentralClient:
    """Client for querying RingCentral Call Logs and Extensions."""

    DEFAULT_SERVER_URL = "https://platform.ringcentral.com"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        jwt_token: Optional[str] = None,
        server_url: Optional[str] = None,
        access_token: Optional[str] = None,
        employee_mapping: Optional[Dict[str, str]] = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.jwt_token = jwt_token
        self.server_url = (server_url or self.DEFAULT_SERVER_URL).rstrip("/")
        self._access_token = access_token
        self.employee_mapping = employee_mapping or {}

    def authenticate_jwt(self) -> str:
        """Exchanges JWT token for an active OAuth2 access token."""
        if not self.jwt_token or not self.client_id or not self.client_secret:
            raise ValueError("JWT authentication requires client_id, client_secret, and jwt_token.")

        token_url = f"{self.server_url}/restapi/oauth/token"
        data = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.jwt_token,
        }).encode("utf-8")

        import base64
        basic_auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("utf-8")

        req = urllib.request.Request(
            token_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic_auth}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            self._access_token = body.get("access_token")
            return self._access_token

    @classmethod
    def from_env(cls) -> RingCentralClient:
        """Instantiates client from standard environment variables."""
        import os
        return cls(
            client_id=os.environ.get("RINGCENTRAL_CLIENT_ID"),
            client_secret=os.environ.get("RINGCENTRAL_CLIENT_SECRET"),
            jwt_token=os.environ.get("RINGCENTRAL_JWT"),
            server_url=os.environ.get("RINGCENTRAL_SERVER_URL"),
        )

    def is_configured(self) -> bool:
        """Returns True if API credentials are provided."""
        return bool((self.client_id and self.client_secret and self.jwt_token) or self._access_token)

    def poll_unreturned_missed_calls(
        self,
        lookback_minutes: int = 120,
        sla_minutes: int = 30,
    ) -> List[Dict[str, Any]]:
        """Directly fetches recent call logs and identifies unreturned missed calls exceeding SLA."""
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(minutes=lookback_minutes)
        calls = self.fetch_call_logs(date_from=start_time, date_to=now)
        
        from robie_job_engine.productivity import ProductivityAuditor
        auditor = ProductivityAuditor(sla_warning_minutes=sla_minutes, employee_mapping=self.employee_mapping)
        audit = auditor.generate_audit(calls, reference_time=now)
        
        return [inc for inc in audit.get("incidents", []) if inc.get("status") == "ORPHANED_ALERT"]

    def _get_headers(self) -> Dict[str, str]:
        if not self._access_token and self.jwt_token:
            self.authenticate_jwt()
        
        if not self._access_token:
            raise RuntimeError("RingCentral client is not authenticated (missing access_token).")

        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    def get_extension_mapping(self) -> Dict[str, str]:
        """Fetches active extensions from RingCentral and maps extension numbers/IDs to employee names."""
        url = f"{self.server_url}/restapi/v1.0/account/~/extension?status=Enabled&perPage=250"
        req = urllib.request.Request(url, headers=self._get_headers(), method="GET")

        mapping: Dict[str, str] = dict(self.employee_mapping)

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for record in data.get("records", []):
                    ext_num = str(record.get("extensionNumber", "")).strip()
                    ext_id = str(record.get("id", "")).strip()
                    name = record.get("name", "").strip()
                    if name:
                        if ext_num:
                            mapping[ext_num] = name
                        if ext_id:
                            mapping[ext_id] = name
        except Exception:
            # Fallback to predefined mapping if API fails or lacks permission
            pass

        self.employee_mapping = mapping
        return mapping

    def fetch_call_logs(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        view: str = "Detailed",
        per_page: int = 250,
    ) -> List[RingCentralCall]:
        """Fetches call logs from RingCentral within the specified time window."""
        now = datetime.now(timezone.utc)
        start = date_from or (now - timedelta(hours=24))
        end = date_to or now

        params = {
            "dateFrom": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dateTo": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "view": view,
            "perPage": str(per_page),
        }
        query_str = urllib.parse.urlencode(params)
        url = f"{self.server_url}/restapi/v1.0/account/~/call-log?{query_str}"

        req = urllib.request.Request(url, headers=self._get_headers(), method="GET")

        calls: List[RingCentralCall] = []

        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for record in data.get("records", []):
                # Extract extension info if nested
                ext_info = record.get("extension", {})
                ext_id = str(ext_info.get("extensionNumber") or ext_info.get("id") or "")

                raw_dict = {
                    "call_id": record.get("id") or record.get("sessionId"),
                    "direction": record.get("direction"),
                    "result": record.get("result") or record.get("action"),
                    "duration_seconds": record.get("duration", 0),
                    "start_time": record.get("startTime"),
                    "from_number": (record.get("from") or {}).get("phoneNumber", ""),
                    "to_number": (record.get("to") or {}).get("phoneNumber", ""),
                    "extension": ext_id,
                }
                calls.append(RingCentralCall.from_dict(raw_dict, employee_mapping=self.employee_mapping))

        return calls
