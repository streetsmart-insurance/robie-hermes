"""The only module allowed to reach api.bland.ai.

Every check below runs before any socket is opened. A refusal raises and
does not include the API key. Each call performs one HTTP request and
does not retry.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Callable

HOST = "https://api.bland.ai"
CALLS_PATH = "/v1/calls"
TEST_HOST = "hermes-test-01"
_TIMEOUT_S = 30


class BlandTransportRefused(RuntimeError):
    """Raised before any network I/O. The message never includes an API key."""


class BlandTransportError(RuntimeError):
    """A single HTTP attempt failed. No retry is performed here."""

    def __init__(self, message: str, *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body if isinstance(body, dict) else {}


def post_call(
    body,
    *,
    api_key: str,
    execute: bool,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
    urlopen: Callable[..., Any] | None = None,
) -> dict:
    """POST /v1/calls once. Refuses unless every Test gate passes."""
    _authorize(execute=execute, env=env, hostname=hostname)
    payload = _require_post_body(body)
    return _send(
        "POST",
        CALLS_PATH,
        payload,
        api_key=api_key,
        urlopen=urlopen,
    )


def get_call(
    call_id,
    *,
    api_key: str,
    execute: bool,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
    urlopen: Callable[..., Any] | None = None,
) -> dict:
    """GET /v1/calls/{call_id} once. Same gates as post_call, no retry."""
    _authorize(execute=execute, env=env, hostname=hostname)
    token = _require_call_id(call_id)
    return _send(
        "GET",
        f"{CALLS_PATH}/{token}",
        None,
        api_key=api_key,
        urlopen=urlopen,
    )


def authorize_live_call(
    *,
    execute: bool,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> None:
    """Refuse unless every live-call gate passes. Does not open a socket."""
    _authorize(execute=execute, env=env, hostname=hostname)


def _authorize(*, execute: bool, env: Mapping[str, str] | None, hostname: str | None) -> None:
    if execute is not True:
        raise BlandTransportRefused("execute must be true")
    source = os.environ if env is None else env
    if source.get("ROBIE_ENV") != "TEST":
        raise BlandTransportRefused("ROBIE_ENV must be TEST")
    host = socket.gethostname() if hostname is None else hostname
    if not isinstance(host, str) or host.split(".")[0] != TEST_HOST:
        raise BlandTransportRefused("Test host hermes-test-01 required")
    if source.get("ROBIE_PHONE_LIVE_CALLS") != "1":
        raise BlandTransportRefused("ROBIE_PHONE_LIVE_CALLS must be 1")


def _require_post_body(body) -> dict:
    if not isinstance(body, dict):
        raise BlandTransportRefused("call body must be an object")
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        raise BlandTransportRefused("voice required")
    if "max_duration" not in body or not _duration_allowed(body.get("max_duration")):
        raise BlandTransportRefused("max_duration must be present and at most 1 minute")
    return body


def _duration_allowed(value) -> bool:
    # Bland max_duration is minutes. Test cap is 60 seconds, which is 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 < value <= 1


def _require_call_id(call_id) -> str:
    if not isinstance(call_id, str):
        raise BlandTransportRefused("call_id required")
    token = call_id.strip()
    if not token or any(char in token for char in "/?#& "):
        raise BlandTransportRefused("call_id required")
    return token


def _send(method: str, path: str, body: dict | None, *, api_key: str, urlopen) -> dict:
    opener = urllib.request.urlopen if urlopen is None else urlopen
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        HOST + path,
        data=data,
        method=method,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        parsed = _json_object(_read_quiet(exc))
        raise BlandTransportError(
            f"Bland HTTP {int(exc.code or 0)}",
            status=int(exc.code or 0),
            body=parsed,
        ) from None
    except AssertionError:
        raise
    except Exception as exc:
        raise BlandTransportError(f"Bland transport failed: {type(exc).__name__}") from None
    parsed = _json_object(raw)
    if not parsed and raw not in (b"", b"{}", None):
        raise BlandTransportError("Bland response was not an object")
    return parsed


def _read_quiet(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read()
    except Exception:
        return b""


def _json_object(raw) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, AttributeError, UnicodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
