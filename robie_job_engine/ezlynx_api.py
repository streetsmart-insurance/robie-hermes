"""EZLynx Documents API client (DocumentApi scope).

Read-only document retrieval through the EZLynx vendor API. This is the
operational path the verification workers use to pull renewal, audit,
mortgagee, and policy-change documents without driving the EZLynx portal
in a browser.

Authentication uses the OAuth2 ``vendor_data_access`` grant against the
EZLynx token endpoint. Credentials are loaded only from a Secret Manager
JSON version reference; they are never accepted in a Job payload, logged,
or written to disk. Error messages are redacted.

Environment mapping (must match ROBIE_ENV):
  TEST        -> UAT credentials (app.uatezlynx.com)
  PRODUCTION  -> Production credentials (app.ezlynx.com)

DocumentApi endpoint paths are confirmed by
``scripts/probe_ezlynx_document_api.py`` before any worker calls them;
until then, workers use the generic :meth:`EzlynxApiClient.api_get` with
a probed path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib import error, parse, request
from urllib.parse import quote, urlparse

from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .secret_manager import GoogleSecretManagerAccessor, SecretAccessor
from .secrets import redact_text
from .write_markers import record_write_marker

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
CLASSIC_TOKEN_KEYS = ("ez_token", "EZToken")
CLASSIC_SECRET_KEYS = ("ez_app_secret", "EZAppSecret")
CLASSIC_USER_KEYS = ("account_username", "AccountUsername")
CLASSIC_BASE_KEYS = ("classic_base_url", "classic_document_base_url")
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

    def __repr__(self) -> str:
        return (
            "EzlynxApiConfig(token_endpoint=<redacted>, "
            "document_base_url=<redacted>, client_id=<redacted>, "
            "client_secret=<redacted>, username=<redacted>, "
            "integration_group_id=<redacted>, scope=<redacted>, "
            "classic_base_url=<redacted>, ez_token=<redacted>, "
            "ez_app_secret=<redacted>, account_username=<redacted>)"
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


def _urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int):
    req = request.Request(url, data=data, headers=headers)
    return request.urlopen(req, timeout=timeout)


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
    ) -> None:
        self._config = config
        self._urlopen = urlopen or _urlopen
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at: float = 0.0

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
            "username": self._config.username,
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
        # Slice 4: every successful API mutation records a worker-unforgeable
        # write marker. Token acquisition is authentication, not a mutation of
        # a system of record, so the token endpoint is excluded.
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE") and url != (
            self._config.token_endpoint or ""
        ):
            record_write_marker(method=f"ezlynx_api.{method.upper()}", url=url)
        return parsed

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

    def list_applicant_documents(
        self,
        applicant_id: str,
        page_index: int = 1,
        page_size: int = 200,
        policy_id: int = 0,
    ) -> dict[str, Any]:
        """Classic GET documentlibrary/list. Do not use OAuth DocumentApi for list."""
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
