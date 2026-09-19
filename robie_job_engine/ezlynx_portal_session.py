"""EZLynx Portal session-cookie HTTP (the working Ascend NOC apply path).

Ralph 2026-09-18: SSRobie can apply org label exactly ``Ascend NOC`` to
Buster Brown note ``1128873902`` in the EZLynx UI; the label stuck after
reload. Same access as Carlo1. So HTTP 403 from hermes Portal API is the
OAuth ``vendor_data_access`` Bearer token (and/or the applicant-label
endpoint), not a role problem.

DiscussionApi / Notes writes still use that OAuth token and succeed.
Portal ``OrganizationLabels`` writes do not — the token lacks label
write scope. The UI and the working CDP ``fetch()`` send the persistent
EZLynx browser session cookies instead.

This module builds that Cookie header from CDP and POSTs Portal JSON
without an ``Authorization`` header. Cookie values are never logged.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib import error
from urllib.parse import urlparse

from .secrets import redact_text


def _api_error(status: int | None, message: str) -> Exception:
    from .ezlynx_api import EzlynxApiError

    return EzlynxApiError(status, message)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
CDP_URL_ENV = "ROBIE_BROWSER_CDP_URL"
EZLYNX_HOST_MARKERS = ("ezlynx.com", "uatezlynx.com")
AUTH_PATH_CDP_SESSION = "cdp_session_cookie"
AUTH_PATH_OAUTH_BEARER = "oauth_bearer"

# XSRF cookie names the EZLynx SPA may set. Header is sent only when the
# matching cookie is already on the CDP session — never invented.
_XSRF_COOKIE_NAMES = frozenset(
    {
        "xsrf-token",
        "x-xsrf-token",
        "xsrftoken",
        "requestverificationtoken",
        "__requestverificationtoken",
    }
)


def cdp_url() -> str:
    return str(os.environ.get(CDP_URL_ENV) or DEFAULT_CDP_URL).rstrip("/")


def is_ezlynx_cookie_domain(domain: str) -> bool:
    host = str(domain or "").strip().lstrip(".").casefold()
    return any(host == marker or host.endswith("." + marker) for marker in EZLYNX_HOST_MARKERS)


def format_cookie_header(cookies: list[dict[str, Any]] | None) -> str:
    """``name=value`` pairs for EZLynx hosts only. Never logs values."""
    parts: list[str] = []
    for row in cookies or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        domain = str(row.get("domain") or "")
        if domain and not is_ezlynx_cookie_domain(domain):
            continue
        parts.append(f"{name}={row.get('value') or ''}")
    return "; ".join(parts)


def portal_session_headers(
    cookie_header: str,
    origin: str,
    cookies: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Same-origin Portal headers. No OAuth Bearer. No ``x-ezlynx-user``.

    The 2026-09-18 sim sent SSRobie Bearer + ``x-ezlynx-user: u438318``
    and still got 403. The working UI ``fetch()`` sends cookies only.
    """
    cookie = str(cookie_header or "").strip()
    if not cookie:
        raise _api_error(None, "EZLynx Portal session cookie header is empty")
    parsed = urlparse(str(origin or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise _api_error(None, "EZLynx Portal origin is required")
    origin_url = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": origin_url,
        "Referer": origin_url + "/web/",
    }
    for row in cookies or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().casefold()
        value = str(row.get("value") or "").strip()
        if not value:
            continue
        if name in _XSRF_COOKIE_NAMES or "antiforgery" in name:
            headers["X-XSRF-TOKEN"] = value
    return headers


def _response_status(resp: Any) -> int | None:
    for attr in ("getcode", "status", "code", "status_code"):
        try:
            if attr == "getcode":
                val = resp.getcode()
            else:
                val = getattr(resp, attr, None)
            if isinstance(val, int) and 100 <= val <= 599:
                return val
        except Exception:  # noqa: BLE001 - try the next status attribute
            continue
    return None


def portal_session_json(
    urlopen: Callable[..., Any],
    method: str,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int = 60,
) -> Any:
    """POST/PUT Portal JSON with session cookies. Fail-closed on HTTP 403.

    Does not attach Bearer. Error messages are redacted and never include
    the Cookie header or response body (bodies can echo session state).
    """
    verb = str(method or "POST").strip().upper() or "POST"
    data = json.dumps(payload).encode("utf-8")
    try:
        resp = urlopen(url, data=data, headers=headers, timeout=timeout)
    except error.HTTPError as exc:
        status = int(exc.code)
        raise _api_error(
            status,
            redact_text(f"EZLynx Portal API {verb} failed: HTTP {status}"),
        ) from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise _api_error(None, "EZLynx Portal API transport failed") from exc
    raw = resp.read() if resp is not None else b""
    status = _response_status(resp)
    if status is not None and status >= 400:
        raise _api_error(
            status,
            redact_text(f"EZLynx Portal API {verb} failed: HTTP {status}"),
        )
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _api_error(None, "EZLynx Portal API returned non-JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise _api_error(None, "EZLynx Portal API returned unexpected shape")
    return parsed


def load_cdp_session_cookies(
    *,
    connect: Callable[[str], list[dict[str, Any]]] | None = None,
    endpoint: str | None = None,
) -> list[dict[str, Any]]:
    """Read EZLynx cookies from the persistent Chrome session. Never logs values."""
    if connect is not None:
        return list(connect(endpoint or cdp_url()) or [])
    return _playwright_cdp_cookies(endpoint or cdp_url())


def load_cdp_session_cookie_header(
    *,
    connect: Callable[[str], list[dict[str, Any]]] | None = None,
    endpoint: str | None = None,
) -> str:
    cookies = load_cdp_session_cookies(connect=connect, endpoint=endpoint)
    header = format_cookie_header(cookies)
    if not header:
        raise _api_error(
            None,
            "CDP EZLynx session has no cookies; Portal org-label apply is fail-closed",
        )
    return header


def _playwright_cdp_cookies(endpoint: str) -> list[dict[str, Any]]:
    """Same persistent browser the working CDP ``fetch()`` uses. Read-only."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise _api_error(
            None,
            "Playwright is required to read the EZLynx CDP session cookies",
        ) from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
        try:
            if not browser.contexts:
                raise _api_error(
                    None,
                    "CDP EZLynx browser has no context; Portal org-label apply is fail-closed",
                )
            raw = browser.contexts[0].cookies()
        finally:
            browser.close()
    return [row for row in raw if isinstance(row, dict)]
