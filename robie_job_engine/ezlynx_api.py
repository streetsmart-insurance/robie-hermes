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

    def __repr__(self) -> str:
        return (
            "EzlynxApiConfig(token_endpoint=<redacted>, "
            "document_base_url=<redacted>, client_id=<redacted>, "
            "client_secret=<redacted>, username=<redacted>, "
            "integration_group_id=<redacted>, scope=<redacted>)"
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
    )


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
        if not isinstance(parsed, dict):
            raise EzlynxApiError(None, "EZLynx API returned unexpected shape")
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
        return self._request_json("GET", url, data=None, headers=headers, timeout=timeout)

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
