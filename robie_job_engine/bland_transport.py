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
PROD_HOST = "hermes-poc-01"
ALLOWED_HOSTS_ENV = "ROBIE_BLAND_ALLOWED_HOSTS"
ALLOWED_ENVS_ENV = "ROBIE_BLAND_ALLOWED_ENVS"
MAX_DURATION_ENV = "ROBIE_BLAND_MAX_DURATION_MINUTES"
KILL_SWITCH_ENV = "ROBIE_BLAND_KILL_SWITCH"
KILL_SWITCH_SECRET = "bland-dispatcher-kill-switch"
BLAND_KEY_SECRET_PROD = "bland-api-key"
BLAND_KEY_SECRET_TEST = "robie-test-bland-api-key"
DEFAULT_ALLOWED_HOSTS = (TEST_HOST,)
DEFAULT_ALLOWED_ENVS = ("TEST",)
DEFAULT_MAX_DURATION_MINUTES = 1
MAX_CONFIGURABLE_DURATION_MINUTES = 12
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
    payload = _require_post_body(body, env)
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


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _csv_tuple(source: Mapping[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(source.get(key) or "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def allowed_hosts(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Hosts that may open a Bland socket. Default is the Test VM only."""
    return _csv_tuple(_source(env), ALLOWED_HOSTS_ENV, DEFAULT_ALLOWED_HOSTS)


def allowed_envs(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """ROBIE_ENV values that may dial. Default is TEST only."""
    return _csv_tuple(_source(env), ALLOWED_ENVS_ENV, DEFAULT_ALLOWED_ENVS)


def max_duration_minutes(env: Mapping[str, str] | None = None) -> float:
    """Bland max_duration cap in minutes. Default is 1. Garbage config stays at 1."""
    raw = str(_source(env).get(MAX_DURATION_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_DURATION_MINUTES
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_DURATION_MINUTES
    if isinstance(value, bool) or value <= 0 or value > MAX_CONFIGURABLE_DURATION_MINUTES:
        return DEFAULT_MAX_DURATION_MINUTES
    return value


def bland_api_key_secret(env: Mapping[str, str] | None = None) -> str:
    """Secret name for the Bland key. Prod and Test use different secrets."""
    if _source(env).get("ROBIE_ENV") == "PRODUCTION":
        return BLAND_KEY_SECRET_PROD
    return BLAND_KEY_SECRET_TEST


def kill_switch_engaged(
    env: Mapping[str, str] | None = None,
    secret_reader: Callable[[str], str] | None = None,
) -> bool:
    """True when the dispatcher kill switch says not to dial.

    The env flag is enough to halt. When a secret reader is supplied, an
    unreadable bland-dispatcher-kill-switch also halts. The reader is not
    called unless the caller passes one. The secret value is never logged.
    """
    source = _source(env)
    if source.get(KILL_SWITCH_ENV) == "1":
        return True
    if secret_reader is None:
        return False
    try:
        raw = secret_reader(KILL_SWITCH_SECRET)
    except Exception:
        return True
    return str(raw or "").strip().lower() in {"1", "true", "on", "halt", "stopped", "kill"}


def _authorize(*, execute: bool, env: Mapping[str, str] | None, hostname: str | None) -> None:
    if execute is not True:
        raise BlandTransportRefused("execute must be true")
    source = _source(env)
    if kill_switch_engaged(source):
        raise BlandTransportRefused("bland-dispatcher-kill-switch is engaged")
    envs = allowed_envs(source)
    if source.get("ROBIE_ENV") not in envs:
        if envs == DEFAULT_ALLOWED_ENVS:
            raise BlandTransportRefused("ROBIE_ENV must be TEST")
        raise BlandTransportRefused("ROBIE_ENV is not an allowed Bland environment")
    host = socket.gethostname() if hostname is None else hostname
    short = host.split(".")[0] if isinstance(host, str) else ""
    hosts = allowed_hosts(source)
    if short not in hosts:
        if hosts == DEFAULT_ALLOWED_HOSTS:
            raise BlandTransportRefused("Test host hermes-test-01 required")
        raise BlandTransportRefused("host is not an allowed Bland host")
    if source.get("ROBIE_PHONE_LIVE_CALLS") != "1":
        raise BlandTransportRefused("ROBIE_PHONE_LIVE_CALLS must be 1")


def _require_post_body(body, env: Mapping[str, str] | None = None) -> dict:
    if not isinstance(body, dict):
        raise BlandTransportRefused("call body must be an object")
    voice = body.get("voice")
    if not isinstance(voice, str) or not voice.strip():
        raise BlandTransportRefused("voice required")
    cap = max_duration_minutes(env)
    if "max_duration" not in body or not _duration_allowed(body.get("max_duration"), cap):
        if cap == DEFAULT_MAX_DURATION_MINUTES:
            raise BlandTransportRefused("max_duration must be present and at most 1 minute")
        raise BlandTransportRefused(
            f"max_duration must be present and at most {cap:g} minutes"
        )
    return body


def _duration_allowed(value, cap: float = DEFAULT_MAX_DURATION_MINUTES) -> bool:
    # Bland max_duration is minutes. The default cap is 1 minute.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 < value <= cap


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
