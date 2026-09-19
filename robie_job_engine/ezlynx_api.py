"""EZLynx OAuth client (DocumentApi + PolicyApi + DiscussionApi).

DocumentApi is the authoritative documents list + download path for Chat
destination verification. Classic ``documentlibrary/list`` may still list,
but classic download is not used for dest evidence (it 500s with
"Authorization code not run").

Proven 2026-09-11 on hermes-poc-01 as agency user SSRobie against ROBIE
Test applicant 220250093 (Freshdesk #2020636 / Applied Gazala):

  GET  {host}/documentapi/documents/v1/account/{ApplicantID}/document-search
  GET  {host}/documentapi/documents/v1/{DocumentID}/download
  POST {host}/DocumentApi/documents/v1/account/{ApplicantID}/document

Use ``results[].id``. Never ``documentUrl`` (old/wrong base URL).
Upload is write-gated to the ROBIE Test allowlist (220250093).

Authentication uses the OAuth2 ``vendor_data_access`` grant against the
EZLynx token endpoint. The Secret Manager ``username`` field is the
agency user (SSRobie). Never authenticate DocumentApi as vendor user
``ssr_userPROD`` — that 403s on agency applicants. ``vendor_username``
may exist in the JSON for other uses and is never sent on the token
form. The JSON password is the vendor password and is not loaded here;
classic SSRobie login stays ``ezlynx-username`` / ``ezlynx-password``.

Credentials are loaded only from a Secret Manager JSON version
reference; they are never accepted in a Job payload, logged, or written
to disk. Error messages are redacted.

Environment mapping (must match ROBIE_ENV):
  TEST        -> UAT credentials (app.uatezlynx.com)
  PRODUCTION  -> Production credentials (app.ezlynx.com)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request
from urllib.parse import quote, urlparse

from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .secret_manager import GoogleSecretManagerAccessor, SecretAccessor
from .secrets import redact_text

ENV_UAT_SECRET = "ROBIE_EZLYNX_API_UAT_SECRET"
ENV_PROD_SECRET = "ROBIE_EZLYNX_API_PROD_SECRET"

GRANT_TYPE = "vendor_data_access"
TOKEN_EXPIRY_SKEW_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 60

REQUIRED_CONFIG_FIELDS = (
    "client_id",
    "client_secret",
    "username",
    "integration_group_id",
    "token_endpoint",
    "document_base_url",
    "scope",
)
# Classic documentlibrary/list auth. Optional in the Secret Manager JSON.
# Missing values raise a clear error only when a classic list is attempted.
# Classic SSRobie login is ezlynx-username / ezlynx-password, not this JSON.
CLASSIC_TOKEN_KEYS = ("ez_token", "EZToken")
CLASSIC_SECRET_KEYS = ("ez_app_secret", "EZAppSecret")
CLASSIC_USER_KEYS = ("account_username", "AccountUsername")
CLASSIC_BASE_KEYS = ("classic_base_url", "classic_document_base_url")
VENDOR_USERNAME_KEYS = ("vendor_username", "VendorUsername")
# Vendor integration user. DocumentApi 403s on agency applicants with this.
VENDOR_DOCUMENT_API_USERNAMES = frozenset({"ssr_userprod"})

# Proven Production/UAT path shapes. Hosts: app.ezlynx.com / app.uatezlynx.com.
DOCUMENT_API_SEARCH_PATH = "/documentapi/documents/v1/account/{applicant_id}/document-search"
DOCUMENT_API_DOWNLOAD_PATH = "/documentapi/documents/v1/{document_id}/download"
DOCUMENT_API_UPLOAD_PATH = "/DocumentApi/documents/v1/account/{applicant_id}/document"
DEFAULT_POLICY_MASTER_ID = "0"
DOCUMENT_RECORD_KEYS = (
    "Records",
    "records",
    "DocumentList",
    "Documents",
    "documents",
    "Items",
    "items",
    "Data",
    "data",
)


class EzlynxApiConfigurationError(RuntimeError):
    """The integration is disabled, misconfigured, or missing its secret reference."""


class EzlynxApiError(RuntimeError):
    """A sanitized EZLynx API failure."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        super().__init__(redact_text(message))

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or bool(
            self.status and self.status >= 500
        )


@dataclass(frozen=True, repr=False)
class EzlynxApiConfig:
    """Resolved, non-secret-shape API configuration. Secrets stay in Secret Manager."""

    token_endpoint: str
    document_base_url: str
    client_id: str
    client_secret: str
    username: str
    integration_group_id: str
    scope: str
    classic_base_url: str = ""
    ez_token: str = ""
    ez_app_secret: str = ""
    account_username: str = ""
    vendor_username: str = ""

    def __repr__(self) -> str:
        return (
            "EzlynxApiConfig(token_endpoint=<redacted>, "
            "document_base_url=<redacted>, client_id=<redacted>, "
            "client_secret=<redacted>, username=<redacted>, "
            "integration_group_id=<redacted>, scope=<redacted>, "
            "classic_base_url=<redacted>, ez_token=<redacted>, "
            "ez_app_secret=<redacted>, account_username=<redacted>, "
            "vendor_username=<redacted>)"
        )


def _secret_env_for(environment: str) -> str:
    if environment == TEST_ENV_NAME:
        return ENV_UAT_SECRET
    if environment in PRODUCTION_ENV_NAMES:
        return ENV_PROD_SECRET
    raise EzlynxApiConfigurationError("ROBIE_ENV must be TEST or PRODUCTION")


def load_ezlynx_api_config(
    environment: str | None = None,
    accessor: SecretAccessor | None = None,
) -> EzlynxApiConfig:
    """Load the EZLynx API configuration for the current ROBIE_ENV.

    Reads a JSON Secret Manager payload with REQUIRED_CONFIG_FIELDS.
    Raises EzlynxApiConfigurationError when the environment or secret
    reference is missing or malformed. Never logs secret values.
    """
    env = (environment or current_robie_env()).strip().upper()
    secret_env = _secret_env_for(env)
    ref = str(os.environ.get(secret_env) or "").strip()
    if not ref:
        raise EzlynxApiConfigurationError(f"{secret_env} must be configured")
    accessor = accessor or GoogleSecretManagerAccessor()
    raw = accessor.access(ref)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise EzlynxApiConfigurationError(
            f"{secret_env} payload is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise EzlynxApiConfigurationError(f"{secret_env} payload must be a JSON object")
    missing = [f for f in REQUIRED_CONFIG_FIELDS if not str(payload.get(f) or "").strip()]
    if missing:
        raise EzlynxApiConfigurationError(
            f"{secret_env} payload missing fields: {', '.join(missing)}"
        )
    return EzlynxApiConfig(
        token_endpoint=str(payload["token_endpoint"]).strip(),
        document_base_url=str(payload["document_base_url"]).strip().rstrip("/") + "/",
        client_id=str(payload["client_id"]).strip(),
        client_secret=str(payload["client_secret"]).strip(),
        username=str(payload["username"]).strip(),
        integration_group_id=str(payload["integration_group_id"]).strip(),
        scope=str(payload["scope"]).strip(),
        classic_base_url=_optional_secret_field(payload, CLASSIC_BASE_KEYS),
        ez_token=_optional_secret_field(payload, CLASSIC_TOKEN_KEYS),
        ez_app_secret=_optional_secret_field(payload, CLASSIC_SECRET_KEYS),
        account_username=_optional_secret_field(payload, CLASSIC_USER_KEYS),
        vendor_username=_optional_secret_field(payload, VENDOR_USERNAME_KEYS),
    )


def _optional_secret_field(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_document_records(payload: Any) -> list[dict[str, Any]]:
    """Pull document rows out of the tenant-varying classic list envelope."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in DOCUMENT_RECORD_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
        if isinstance(candidate, dict):
            nested = extract_document_records(candidate)
            if nested:
                return nested
    return []


def document_display_fields(row: Any) -> dict[str, str]:
    """Normalize a classic document row to display name / description fields."""
    if not isinstance(row, dict):
        return {}
    name = (
        row.get("DocumentName")
        or row.get("documentName")
        or row.get("Name")
        or row.get("name")
        or row.get("FileName")
        or row.get("fileName")
        or row.get("Description")
        or row.get("description")
        or ""
    )
    return {
        "name": str(name or "").strip(),
        "description": str(row.get("Description") or row.get("description") or "").strip(),
        "policy_number": str(
            row.get("PolicyNumber") or row.get("policyNumber") or row.get("policy_number") or ""
        ).strip(),
    }


def is_vendor_document_api_username(username: str) -> bool:
    """True for the vendor integration user that 403s on agency DocumentApi."""
    folded = str(username or "").strip().casefold()
    return folded in VENDOR_DOCUMENT_API_USERNAMES or folded.startswith("ssr_user")


def _document_api_result_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if isinstance(results, list):
        return results
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data, list):
        return data
    return []


def extract_document_api_results(payload: Any) -> list[dict[str, str]]:
    """Normalize DocumentApi search rows. Use ``results[].id`` only.

    ``documentUrl`` is the old/wrong base URL and is never used as the
    identifier, even when ``id`` is missing.
    """
    out: list[dict[str, str]] = []
    for row in _document_api_result_rows(payload):
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        if raw_id is None or isinstance(raw_id, bool):
            continue
        document_id = str(raw_id).strip()
        if not document_id or not document_id.isdigit():
            continue
        name = (
            row.get("documentName")
            or row.get("DocumentName")
            or row.get("name")
            or row.get("Name")
            or row.get("fileName")
            or row.get("FileName")
            or row.get("description")
            or row.get("Description")
            or ""
        )
        out.append({"id": document_id, "name": str(name or "").strip()})
    return out


def parse_uploaded_document_id(raw: bytes) -> str:
    """Parse a DocumentApi upload 200 body. The proven body is a numeric id."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise EzlynxApiError(None, "DocumentApi upload returned empty body")
    parsed: Any
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    if isinstance(parsed, bool):
        raise EzlynxApiError(None, "DocumentApi upload returned a non-numeric document id")
    if isinstance(parsed, int):
        if parsed <= 0:
            raise EzlynxApiError(None, "DocumentApi upload returned a non-numeric document id")
        return str(parsed)
    if isinstance(parsed, str) and parsed.strip().isdigit():
        return parsed.strip()
    raise EzlynxApiError(None, "DocumentApi upload returned a non-numeric document id")


def encode_multipart_document_upload(
    *,
    document_name: str,
    filename: str,
    file_bytes: bytes,
    policy_master_id: str,
    file_content_type: str = "application/octet-stream",
) -> tuple[bytes, str]:
    """Build multipart/form-data for DocumentApi upload. Field names are proven."""
    boundary = "----RobieDocumentApiBoundary" + os.urandom(16).hex()
    crlf = b"\r\n"

    def _text_part(name: str, value: str) -> bytes:
        return (
            f"--{boundary}".encode("ascii")
            + crlf
            + f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            + crlf
            + crlf
            + str(value).encode("utf-8")
            + crlf
        )

    safe_name = _safe_upload_filename(filename)
    header = (
        f"--{boundary}".encode("ascii")
        + crlf
        + (
            f'Content-Disposition: form-data; name="File"; filename="{safe_name}"'
        ).encode("utf-8")
        + crlf
        + f"Content-Type: {file_content_type}".encode("ascii")
        + crlf
        + crlf
    )
    body = (
        _text_part("DocumentName", document_name)
        + header
        + file_bytes
        + crlf
        + _text_part("PolicyMasterId", policy_master_id)
        + f"--{boundary}--".encode("ascii")
        + crlf
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _safe_upload_filename(name: str) -> str:
    base = Path(str(name or "").strip()).name or "document.bin"
    return base.replace('"', "_").replace("\r", "").replace("\n", "")


@dataclass(frozen=True, repr=False)
class EzlynxDocumentDownload:
    """Raw DocumentApi download. Content-Type comes from the HTTP response."""

    document_id: str
    body: bytes
    content_type: str

    def __repr__(self) -> str:
        return (
            f"EzlynxDocumentDownload(document_id={self.document_id!r}, "
            f"body=<{len(self.body)} bytes>, content_type={self.content_type!r})"
        )


def _urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int):
    """HTTP client using http.client directly (not urllib).
    
    The canary script (scripts/canary_create_policy.py) proved that
    http.client.HTTPSConnection correctly handles EZLynx API responses,
    while urllib.request.urlopen returns empty responses with None status
    for the PolicyApi create endpoint. This implementation matches the
    canary's proven approach.
    """
    import http.client
    import urllib.parse
    
    parsed = urllib.parse.urlparse(url)
    # Use HTTPSConnection for https, HTTPConnection for http
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parsed.netloc, timeout=timeout)
    
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    
    # Determine method: POST if data, else GET
    method = "POST" if data is not None else "GET"
    
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        # Read the body now; wrap in a compatible response object
        body = resp.read()
        status = resp.status
        resp_headers = resp.getheaders()
        conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    
    # Return a wrapper that provides the urllib-compatible interface
    # (read, getcode, status, getheaders) with reliable status
    class _HttpClientResponse:
        def __init__(self, body_bytes, http_status, headers_list):
            self._body = body_bytes
            self._status = http_status
            self._headers = headers_list
        
        def read(self):
            return self._body
        
        def getcode(self):
            return self._status
        
        @property
        def status(self):
            return self._status
        
        def getheaders(self):
            return self._headers
        
        def getheader(self, name, default=None):
            for k, v in self._headers:
                if k.lower() == name.lower():
                    return v
            return default
    
    return _HttpClientResponse(body, status, resp_headers)


class EzlynxApiClient:
    """Authenticated EZLynx API client with cached bearer tokens.

    ``urlopen`` is injectable for tests; production uses urllib directly.
    All failures raise EzlynxApiError with redacted messages.
    """

    def __init__(
        self,
        config: EzlynxApiConfig,
        urlopen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        session_cookie_header: str | None = None,
        session_cookie_loader: Callable[[], str] | None = None,
        session_cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self._config = config
        self._urlopen = urlopen or _urlopen
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Portal org-label writes use CDP session cookies, not OAuth Bearer.
        self._session_cookie_header = str(session_cookie_header or "").strip()
        self._session_cookie_loader = session_cookie_loader
        self._session_cookies = list(session_cookies or [])

    def _agency_document_api_username(self) -> str:
        """Secret Manager ``username`` (SSRobie). Never ``vendor_username`` / ssr_userPROD."""
        username = str(self._config.username or "").strip()
        if is_vendor_document_api_username(username):
            raise EzlynxApiConfigurationError(
                "DocumentApi must authenticate as the agency username, not the vendor user"
            )
        if not username:
            raise EzlynxApiConfigurationError("DocumentApi username is missing")
        return username

    def get_token(self) -> str:
        """Return a cached bearer token, refreshing it when expired."""
        now = self._clock()
        if self._token and now < self._token_expires_at:
            return self._token
        form = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": GRANT_TYPE,
            "scope": self._config.scope,
            "username": self._agency_document_api_username(),
            "integration_group_id": self._config.integration_group_id,
        }
        body = self._post_form(
            self._config.token_endpoint, form, authenticated=False
        )
        access_token = str(body.get("access_token") or "").strip()
        if not access_token:
            raise EzlynxApiError(None, "token endpoint returned no access_token")
        try:
            expires_in = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        self._token = access_token
        self._token_expires_at = now + max(expires_in - TOKEN_EXPIRY_SKEW_SECONDS, 60)
        return self._token

    def _post_form(
        self, url: str, form: dict[str, str], *, authenticated: bool
    ) -> dict[str, Any]:
        data = parse.urlencode(form).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.get_token()}"
        return self._request_json("POST", url, data=data, headers=headers)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None,
        headers: dict[str, str],
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        try:
            resp = self._urlopen(url, data=data, headers=headers, timeout=timeout)
            raw = resp.read()
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - best effort detail only
                detail = ""
            raise EzlynxApiError(
                exc.code, f"EZLynx API {method} failed: HTTP {exc.code} {detail}"
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise EzlynxApiError(None, f"EZLynx API {method} transport failed") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EzlynxApiError(None, "EZLynx API returned non-JSON") from exc
        if not isinstance(parsed, (dict, list)):
            raise EzlynxApiError(None, "EZLynx API returned unexpected shape")
        return parsed

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        """Authenticated POST of a JSON payload against the API origin.

        Added for the verified writers (discussion notes). ``path`` is
        relative to the API origin, e.g. ``"/DiscussionApi/discussions/v1/notes"``.
        Fail-closed: transport and HTTP errors raise EzlynxApiError.
        """
        url = self._origin() + "/" + str(path or "").lstrip("/")
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return self._request_json("POST", url, data=data, headers=headers, timeout=timeout)

    def _response_content_type(self, resp: Any) -> str:
        headers = getattr(resp, "headers", None) or {}
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter("Content-Type") or getter("content-type") or "").strip()
        return ""

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None,
        headers: dict[str, str],
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        error_label: str = "EZLynx API",
    ) -> tuple[bytes, str]:
        raw, content_type, _status, _diagnostics = self._request_bytes_with_status(
            method, url, data=data, headers=headers, timeout=timeout, error_label=error_label
        )
        return raw, content_type

    def _request_bytes_with_status(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None,
        headers: dict[str, str],
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        error_label: str = "EZLynx API",
    ) -> tuple[bytes, str, int | None, dict[str, Any]]:
        """Same as _request_bytes but also returns the HTTP status code.

        Returns (body_bytes, content_type, http_status, diagnostics). The
        diagnostics dict contains response_type, response_headers, and
        status_source for fail-closed debugging. On HTTP error,
        raises EzlynxApiError as before (status is in the exception).
        """
        try:
            resp = self._urlopen(url, data=data, headers=headers, timeout=timeout)
            http_status = None
            status_source = None
            resp_type = type(resp).__name__
            # Try multiple status attributes; validate it's a real HTTP code.
            for attr in ("getcode", "status", "code", "status_code"):
                try:
                    if attr == "getcode":
                        val = resp.getcode()
                    else:
                        val = getattr(resp, attr, None)
                    if isinstance(val, int) and 100 <= val <= 599:
                        http_status = val
                        status_source = attr
                        break
                except Exception:
                    continue
            # Capture response headers for diagnostics.
            resp_headers: dict[str, str] = {}
            try:
                if hasattr(resp, "getheaders"):
                    resp_headers = dict(resp.getheaders())
                elif hasattr(resp, "headers"):
                    resp_headers = dict(resp.headers)
            except Exception:
                pass
            diagnostics = {
                "response_type": resp_type,
                "response_headers": resp_headers,
                "status_source": status_source,
            }
            return resp.read(), self._response_content_type(resp), http_status, diagnostics
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - best effort detail only
                detail = ""
            raise EzlynxApiError(
                exc.code, f"{error_label} {method} failed: HTTP {exc.code} {detail}"
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise EzlynxApiError(None, f"{error_label} {method} transport failed") from exc

    def api_get(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Authenticated GET against the DocumentApi base URL. Read-only."""
        base = self._config.document_base_url
        url = base + path.lstrip("/")
        if query:
            url += "?" + parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        parsed = self._request_json("GET", url, data=None, headers=headers, timeout=timeout)
        if not isinstance(parsed, dict):
            raise EzlynxApiError(None, "EZLynx API returned unexpected shape")
        return parsed

    def _origin(self) -> str:
        parsed = urlparse(self._config.document_base_url or self._config.token_endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise EzlynxApiConfigurationError("EZLynx API origin could not be derived")
        return f"{parsed.scheme}://{parsed.netloc}"

    def _wrap_search(self, parsed: Any) -> dict[str, Any]:
        if isinstance(parsed, dict) and "status" in parsed and "data" in parsed:
            return parsed
        return {"status": "success", "data": parsed}

    def _classic_auth_headers(self) -> dict[str, str]:
        token = self._config.ez_token
        secret = self._config.ez_app_secret
        username = self._config.account_username
        if not token or not secret or not username:
            raise EzlynxApiConfigurationError(
                "classic EZLynx document library auth is not configured "
                "(Secret Manager payload needs ez_token/EZToken, "
                "ez_app_secret/EZAppSecret, and account_username/AccountUsername)"
            )
        return {
            "EZToken": token,
            "EZAppSecret": secret,
            "AccountUsername": username,
            "Accept": "application/json",
        }

    def search_policy_by_number(self, policy_number: str) -> dict[str, Any]:
        """OAuth GET /PolicyApi/policy/v1/search?PolicyNumber=. Read-only."""
        number = str(policy_number or "").strip()
        if not number:
            raise EzlynxApiError(None, "policy number is required")
        url = (
            self._origin()
            + "/PolicyApi/policy/v1/search?"
            + parse.urlencode({"PolicyNumber": number})
        )
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        parsed = self._request_json("GET", url, data=None, headers=headers)
        return self._wrap_search(parsed)

    def create_policy(
        self,
        *,
        applicant_id: str,
        policy_number: str,
        master_company: int = 13585,
        writing_company: str = "10048",
        lob: str = "HOME",
        effective_date: str,
        expiration_date: str,
        written_premium: float = 1.00,
        rating_state: str = "NJ",
        transaction_type: str = "NBS",
    ) -> dict[str, Any]:
        """OAuth POST /PolicyApi/account/{applicantId}/policy/v1/create.

        The gold payload: writingCompany is the string "10048" and
        masterCompany is the int 13585. Exactly one create attempt is made
        by the caller; this method performs exactly one POST.
        """
        applicant = str(applicant_id or "").strip()
        number = str(policy_number or "").strip()
        if not number:
            raise EzlynxApiError(None, "policy number is required")
        # Enforce the EZLynx write allowlist before any write leaves this box.
        applicant = require_allowed_ezlynx_write_applicant(applicant)
        payload = {
            "accountId": int(applicant),
            "policyNumber": number,
            "writingCompany": str(writing_company),
            "lob": str(lob),
            "effectiveDate": str(effective_date),
            "expirationDate": str(expiration_date),
            "masterCompany": int(master_company),
            "writtenPremium": float(written_premium),
            "ratingState": str(rating_state),
            "transactionType": str(transaction_type),
            "acordXml": "",
        }
        url = self._origin() + f"/PolicyApi/account/{applicant}/policy/v1/create"
        headers = {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }
        data = json.dumps(payload).encode("utf-8")
        # The create endpoint returns the new policy id as a bare scalar
        # (string or number), not a JSON object — parse flexibly.
        # Capture raw HTTP details for fail-closed diagnostics: the caller
        # needs the status code and raw body when no policy ID comes back.
        raw, _content_type, http_status, diagnostics = self._request_bytes_with_status(
            "POST", url, data=data, headers=headers, error_label="EZLynx PolicyApi create"
        )
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = text.strip('"')
        return {
            "request_payload": payload,
            "response": parsed,
            "http_status": http_status,
            "raw_body": text,
            "url": url,
            "response_type": diagnostics.get("response_type"),
            "response_headers": diagnostics.get("response_headers"),
            "status_source": diagnostics.get("status_source"),
        }

    def list_applicant_documents(
        self,
        applicant_id: str,
        page_index: int = 1,
        page_size: int = 200,
        policy_id: int = 0,
    ) -> dict[str, Any]:
        """Classic GET documentlibrary/list.

        Destination verification does not use this path. Authoritative docs
        evidence is DocumentApi search + download by ``results[].id``.
        """
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise EzlynxApiError(None, "applicant id is required")
        base = str(self._config.classic_base_url or "").strip()
        if not base:
            base = self._origin() + "/ezlynxapi/"
        base = base.rstrip("/") + "/"
        path = (
            f"api/documentlibrary/list/{quote(applicant, safe='')}/"
            f"{int(page_index)}/{int(page_size)}/{int(policy_id)}"
        )
        parsed = self._request_json(
            "GET",
            base + path,
            data=None,
            headers=self._classic_auth_headers(),
        )
        if isinstance(parsed, dict):
            return parsed
        return {"Records": parsed}

    def get_applicant_discussions(
        self,
        applicant_id: str,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Prefer DiscussionApi OAuth. Receipt only — empty list if unavailable."""
        del page_size
        applicant = str(applicant_id or "").strip()
        if not applicant:
            return []
        url = (
            self._origin()
            + "/DiscussionApi/discussion/v1/applicant/"
            + quote(applicant, safe="")
        )
        try:
            parsed = self._request_json(
                "GET",
                url,
                data=None,
                headers={"Authorization": f"Bearer {self.get_token()}"},
            )
        except EzlynxApiError:
            return []
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
        if isinstance(parsed, dict):
            for key in ("Discussions", "discussions", "Items", "items", "Data", "data"):
                candidate = parsed.get(key)
                if isinstance(candidate, list):
                    return [row for row in candidate if isinstance(row, dict)]
            if parsed.get("Title") or parsed.get("title") or parsed.get("Subject"):
                return [parsed]
        return []

    def download_bytes(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> bytes:
        """Authenticated GET returning raw bytes (for document downloads)."""
        base = self._config.document_base_url
        url = base + path.lstrip("/")
        if query:
            url += "?" + parse.urlencode(query, doseq=True)
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        try:
            resp = self._urlopen(url, data=None, headers=headers, timeout=timeout)
            return resp.read()
        except error.HTTPError as exc:
            raise EzlynxApiError(
                exc.code, f"EZLynx API download failed: HTTP {exc.code}"
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise EzlynxApiError(None, "EZLynx API download transport failed") from exc

    def search_applicant_documents(self, applicant_id: str) -> dict[str, Any]:
        """OAuth GET DocumentApi document-search. Read-only.

        Proven path: ``/documentapi/documents/v1/account/{ApplicantID}/document-search``.
        Callers must use ``results[].id``, never ``documentUrl``.
        """
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise EzlynxApiError(None, "applicant id is required")
        url = self._origin() + DOCUMENT_API_SEARCH_PATH.format(
            applicant_id=quote(applicant, safe="")
        )
        headers = {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept": "application/json",
        }
        parsed = self._request_json("GET", url, data=None, headers=headers)
        if isinstance(parsed, dict):
            return parsed
        return {"results": parsed}

    def download_document(self, document_id: str) -> EzlynxDocumentDownload:
        """OAuth GET DocumentApi download. Read-only. Returns file bytes.

        Proven path: ``/documentapi/documents/v1/{DocumentID}/download``.
        ``documentUrl`` from search is never followed.
        """
        doc_id = str(document_id or "").strip()
        if not doc_id or not doc_id.isdigit():
            raise EzlynxApiError(None, "document id is required")
        url = self._origin() + DOCUMENT_API_DOWNLOAD_PATH.format(
            document_id=quote(doc_id, safe="")
        )
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        body, content_type = self._request_bytes(
            "GET",
            url,
            data=None,
            headers=headers,
            error_label="EZLynx DocumentApi",
        )
        if not body:
            raise EzlynxApiError(None, "DocumentApi download returned empty body")
        return EzlynxDocumentDownload(
            document_id=doc_id, body=body, content_type=content_type
        )

    def upload_applicant_document(
        self,
        applicant_id: str,
        document_name: str,
        file_bytes: bytes,
        *,
        filename: str | None = None,
        policy_master_id: str | int | None = None,
        file_content_type: str = "application/octet-stream",
    ) -> str:
        """OAuth POST DocumentApi upload. Write-gated by ezlynx_write_scope.

        Proven path: ``/DocumentApi/documents/v1/account/{ApplicantID}/document``.
        Multipart fields: DocumentName, File, PolicyMasterId (default ``0``).
        200 body is a numeric document id. Applicant eligibility follows
        ``ROBIE_EZLYNX_WRITE_APPLICANT_IDS`` (unset/empty = agency-wide).
        """
        applicant = require_allowed_ezlynx_write_applicant(applicant_id)
        name = str(document_name or "").strip()
        if not name:
            raise EzlynxApiError(None, "document name is required")
        if not file_bytes:
            raise EzlynxApiError(None, "document bytes are required")
        master = str(
            DEFAULT_POLICY_MASTER_ID if policy_master_id in (None, "") else policy_master_id
        ).strip()
        if not master:
            master = DEFAULT_POLICY_MASTER_ID
        url = self._origin() + DOCUMENT_API_UPLOAD_PATH.format(
            applicant_id=quote(applicant, safe="")
        )
        body, content_type = encode_multipart_document_upload(
            document_name=name,
            filename=filename or name,
            file_bytes=file_bytes,
            policy_master_id=master,
            file_content_type=file_content_type,
        )
        headers = {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": content_type,
            "Accept": "text/plain",
        }
        raw, _ = self._request_bytes(
            "POST",
            url,
            data=body,
            headers=headers,
            error_label="EZLynx DocumentApi",
        )
        return parse_uploaded_document_id(raw)

    def list_organization_labels(self) -> list[dict[str, Any]]:
        """OAuth GET of agency org labels. Read-only. Never creates a label.

        Path: ``/EZLynxPortalAPI/Organizations/GetOrganizationLabels``.
        This is HTTP API (Bearer), not Playwright.
        """
        from .ezlynx_org_labels import ORG_LABELS_LIST_PATH, normalize_org_label_rows

        url = (
            self._origin()
            + ORG_LABELS_LIST_PATH
            + "?"
            + parse.urlencode({"includeNonActive": "false"})
        )
        headers = {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept": "application/json",
        }
        parsed = self._request_json("GET", url, data=None, headers=headers)
        return normalize_org_label_rows(parsed)

    def apply_applicant_organization_label(
        self, applicant_id: str, label_id: str
    ) -> dict[str, Any]:
        """OAuth POST of one org label onto an applicant.

        Proven 2026-09-18: this Bearer Portal write returns HTTP 403 for
        SSRobie (``x-ezlynx-user u438318``). DiscussionApi note writes with
        the same token succeed. Do not use this for Ascend NOC — call
        :meth:`apply_note_organization_label` (CDP session cookies).
        Kept so tests can assert the 403 is fail-closed.
        """
        from .ezlynx_org_labels import applicant_labels_path

        applicant = require_allowed_ezlynx_write_applicant(applicant_id)
        resolved_id = str(label_id or "").strip()
        if not resolved_id:
            raise EzlynxApiError(None, "organization label id is required")
        parsed = self.post_json(
            applicant_labels_path(applicant),
            {
                "applicantId": applicant,
                "organizationLabelIds": [resolved_id],
            },
        )
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}

    def _portal_session_cookie_header(self) -> str:
        if self._session_cookie_header:
            return self._session_cookie_header
        if self._session_cookie_loader is not None:
            header = str(self._session_cookie_loader() or "").strip()
            if header:
                return header
        from .ezlynx_portal_session import load_cdp_session_cookie_header

        return load_cdp_session_cookie_header()

    def apply_note_organization_label(
        self, note_id: str, label_id: str
    ) -> dict[str, Any]:
        """Apply one org label onto a discussion note via CDP session cookies.

        Path: ``POST /EZLynxPortalAPI/Notes/{noteId}/OrganizationLabels``.
        This is the UI / working-CDP path for Activities-enabled ``Ascend NOC``.
        No OAuth Bearer. No Playwright clicking. Fail-closed on HTTP 403.
        """
        from .ezlynx_org_labels import note_labels_path
        from .ezlynx_portal_session import portal_session_headers, portal_session_json

        note = str(note_id or "").strip()
        resolved_id = str(label_id or "").strip()
        if not note:
            raise EzlynxApiError(None, "note id is required for organization label apply")
        if not resolved_id:
            raise EzlynxApiError(None, "organization label id is required")
        origin = self._origin()
        headers = portal_session_headers(
            self._portal_session_cookie_header(),
            origin,
            cookies=self._session_cookies or None,
        )
        parsed = portal_session_json(
            self._urlopen,
            "POST",
            origin + note_labels_path(note),
            {"organizationLabelIds": [resolved_id]},
            headers,
        )
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
