"""EZLynx portal endpoint clients for the Weekly Expiration List report.

All five endpoints below were PROVEN live 2026-10-05 via the box's
persistent Chrome CDP session (read-only GET/POST with session cookies,
no navigation, no writes):

  POST /EZLynxRenewalCenter/ExpirationList/GetExpirationList
       (params in query string; JSON body tolerated)
  GET  /ApplicantApi/v1/sidebar?applicantId=ID
  GET  /PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=ID
  GET  /EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=&pageSize=&applicantId=&applicantContext=
  GET  /EZLynxPortalAPI/Discussions/GetDiscussionDetail?discussionId=&applicantId=

The cookie path reuses robie_job_engine.ezlynx_portal_session (same
helpers the working Ascend NOC label path uses). Cookie values are never
logged. Transport is injectable so unit tests and fixture smoke runs can
substitute recorded responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ... import ezlynx_portal_session as eps
from ...secrets import redact_text


class ExpirationListError(Exception):
    """Fail-closed error for any portal read failure."""


@dataclass
class PortalTransport:
    """HTTP transport over the EZLynx CDP session-cookie path.

    `cookie_connect` is the injectable seam: pass a callable returning the
    raw CDP cookie list (as `load_cdp_session_cookies(connect=...)` takes)
    to run against recorded fixtures without a live browser.
    """

    origin: str = "https://app.ezlynx.com"
    timeout: int = 60
    cookie_connect: Callable[[str], list[dict[str, Any]] | None] | None = None

    def __post_init__(self) -> None:
        self._cookie_header: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._cookie_header is None:
            self._cookie_header = eps.load_cdp_session_cookie_header(
                connect=self.cookie_connect
            )
        return eps.portal_session_headers(self._cookie_header, self.origin)

    def _request(
        self, method: str, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        query = urlparse.urlencode({k: v for k, v in (params or {}).items()})
        url = self.origin.rstrip("/") + path + ("?" + query if query else "")
        data = b"{}" if method.upper() == "POST" else None
        req = urlrequest.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            resp = urlrequest.urlopen(req, timeout=self.timeout)
        except urlerror.HTTPError as exc:
            raise ExpirationListError(
                redact_text(f"EZLynx portal {method} {path} failed: HTTP {exc.code}")
            ) from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise ExpirationListError(
                redact_text(f"EZLynx portal {method} {path} transport failed")
            ) from exc
        raw = resp.read() if resp is not None else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExpirationListError(
                redact_text(f"EZLynx portal {method} {path} returned non-JSON")
            ) from exc
        if not isinstance(parsed, (dict, list)):
            raise ExpirationListError(
                redact_text(f"EZLynx portal {method} {path} unexpected shape")
            )
        return parsed


@dataclass
class ExpirationPortalClient:
    """The five expiration-list read endpoints. Read-only, fail-closed."""

    transport: PortalTransport = field(default_factory=PortalTransport)

    # -- Retention Center -------------------------------------------------
    def retention_expiration_list(
        self,
        *,
        page_size: int = 100,
        page_index: int = 1,
        sort_column: str = "daysToExpiration",
        sort_order: str = "asc",
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageSize": page_size,
            "pageIndex": page_index,
            "sortColumn": sort_column,
            "sortOrder": sort_order,
        }
        if extra_params:
            params.update(extra_params)
        result = self.transport._request(
            "POST", "/EZLynxRenewalCenter/ExpirationList/GetExpirationList", params
        )
        if not isinstance(result, dict):
            raise ExpirationListError("GetExpirationList returned non-object")
        return result

    def retention_expiration_count(self, **extra_params: Any) -> dict[str, Any]:
        result = self.transport._request(
            "POST",
            "/EZLynxRenewalCenter/ExpirationList/GetExpirationListCount",
            extra_params or None,
        )
        if not isinstance(result, dict):
            raise ExpirationListError("GetExpirationListCount returned non-object")
        return result

    # -- Per-account reads -------------------------------------------------
    def sidebar(self, applicant_id: int) -> dict[str, Any]:
        result = self.transport._request(
            "GET", "/ApplicantApi/v1/sidebar", {"applicantId": applicant_id}
        )
        if not isinstance(result, dict):
            raise ExpirationListError("sidebar returned non-object")
        return result

    def policies(self, applicant_id: int) -> list[dict[str, Any]]:
        result = self.transport._request(
            "GET", "/PolicyAPI/v1/PolicyCard/GetPolicies", {"applicantId": applicant_id}
        )
        if not isinstance(result, dict):
            raise ExpirationListError("GetPolicies returned non-object")
        cards = result.get("policyCards") or []
        if not isinstance(cards, list):
            raise ExpirationListError("GetPolicies.policyCards not a list")
        return cards

    def paged_discussions(
        self, applicant_id: int, *, page_number: int = 1, page_size: int = 60
    ) -> dict[str, Any]:
        result = self.transport._request(
            "GET",
            "/EZLynxPortalAPI/Discussions/GetPagedDiscussions",
            {
                "pageNumber": page_number,
                "pageSize": page_size,
                "applicantId": applicant_id,
                "applicantContext": "true",
            },
        )
        if not isinstance(result, dict):
            raise ExpirationListError("GetPagedDiscussions returned non-object")
        return result

    def discussion_detail(self, discussion_id: int, applicant_id: int) -> dict[str, Any]:
        result = self.transport._request(
            "GET",
            "/EZLynxPortalAPI/Discussions/GetDiscussionDetail",
            {"discussionId": discussion_id, "applicantId": applicant_id},
        )
        if not isinstance(result, dict):
            raise ExpirationListError("GetDiscussionDetail returned non-object")
        return result
