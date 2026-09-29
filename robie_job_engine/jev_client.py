"""Small client for Jev (TypeSafe) end-state scoring.

POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <key>

The key comes from the ``JEV_API_KEY`` environment variable when that is
set, otherwise from Secret Manager secret ``jev-api-key`` in project
``streetsmart-hermes-poc``. The key is never logged and never written
into the job database.

Timeouts are bounded. Transport failures and overloaded responses are
tried once more. Any failure raises ``JevUnavailable`` so the caller can
fail safe (verdict unsure, escalate). A missing key does the same and
does not open a connection.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_SECRET_ID = "jev-api-key"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}


class JevUnavailable(RuntimeError):
    """Jev could not score the job. Callers must not treat this as a pass."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _project() -> str:
    return (
        os.environ.get("GOOGLE_CLOUD_PROJECT", "streetsmart-hermes-poc").strip()
        or "streetsmart-hermes-poc"
    )


def default_jev_secret_resource() -> str:
    return f"projects/{_project()}/secrets/{JEV_SECRET_ID}/versions/latest"


def load_jev_api_key() -> str:
    """Return the Jev key, or an empty string when it cannot be read.

    ``JEV_API_KEY`` wins over Secret Manager. Failures return "" so the
    caller can fail safe. The value is not logged.
    """
    env = os.environ.get("JEV_API_KEY", "").strip()
    if env:
        return env
    resource = os.environ.get("ROBIE_JEV_API_KEY_SECRET", "").strip() or default_jev_secret_resource()
    if JEV_SECRET_ID not in resource:
        return ""
    try:
        from .staff_jobs_common import read_secret

        return str(read_secret(resource) or "").strip()
    except Exception:
        return ""


class JevClient:
    """One evaluation call with a timeout and a single retry."""

    def __init__(
        self,
        api_key: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 20.0,
        retry_sleep: float = 0.4,
        url: str = JEV_URL,
        model: str = JEV_MODEL,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._opener = opener
        self.timeout = timeout
        self.retry_sleep = retry_sleep
        self.url = url
        self.model = model

    def __repr__(self) -> str:
        return "JevClient(api_key=<redacted>)"

    def evaluate(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise JevUnavailable("Jev API key is not configured", retryable=False)
        body = {"state": state, "model": self.model, "questions": questions}
        last: JevUnavailable | None = None
        for attempt in range(2):
            try:
                return self._post(body)
            except JevUnavailable as exc:
                last = exc
                if attempt == 0 and exc.retryable:
                    if self.retry_sleep:
                        time.sleep(self.retry_sleep)
                    continue
                raise
        assert last is not None
        raise last

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self.timeout) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            raise JevUnavailable(
                f"Jev HTTP {status}",
                retryable=status in _RETRYABLE_STATUS,
            ) from None
        except Exception as exc:
            raise JevUnavailable(
                f"Jev request failed: {type(exc).__name__}",
                retryable=True,
            ) from None
        if status >= 400:
            raise JevUnavailable(
                f"Jev HTTP {status}",
                retryable=status in _RETRYABLE_STATUS,
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise JevUnavailable("Jev returned unreadable JSON", retryable=False) from None
        if not isinstance(parsed, dict):
            raise JevUnavailable("Jev returned an unexpected payload", retryable=False)
        return parsed


def build_jev_client() -> JevClient:
    return JevClient(load_jev_api_key())
