"""Explicit, fail-closed Discussion API route for the task flow (intake, canary, field inspector).

Two routes exist and the caller must be on the approved one:
- ``uat``:  ROBIE_ENV=TEST, the UAT secret (``ROBIE_EZLYNX_API_UAT_SECRET``).
- ``live``: the PRODUCTION secret (``ROBIE_EZLYNX_API_PROD_SECRET``) and the live host.

Rules, each tested:
- PRODUCTION is always ``live`` (unchanged behaviour); declaring anything else refuses.
- TEST must DECLARE its route in ``ROBIE_TASK_DISCUSSION_ROUTE``; there is no default. A declared route
  must agree with the environment's own ``ROBIE_EZLYNX_DISCUSSION_API`` switch (live iff ``live``).
- The secret that is read is chosen by the route alone and the host in it must match the route. Any
  failure refuses; it never tries the other secret, never falls back and never guesses.
- The authentication endpoint and API origin must be EXACTLY the approved HTTPS pair for the route
  (no substring checks); every request is re-checked against it and redirects are never followed.
- The client reads and attaches NO browser cookies unless a caller explicitly asks (``browser_session``).
- The task flow never uses the shared SSRobie username/password: the token request carries no
  password, so it is the vendor integration grant only. (The note tool's live route is separate.)
- The client carries a ``route_record`` (route, host, secret resource name, grant), never a secret value.
"""
from __future__ import annotations

import os
from typing import Any
from urllib import request
from urllib.parse import urlparse

from .ezlynx_api import EzlynxApiConfigurationError, load_ezlynx_api_config
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env

ROUTE_ENV = "ROBIE_TASK_DISCUSSION_ROUTE"
ROUTES = ("uat", "live")
# EXACT approved HTTPS pairs (authentication endpoint + API origin), from the repo's own constants.
# No substring matching: scheme, host, port, userinfo, path, query and fragment must all be exact.
APPROVED = {
    "live": {"host": "app.ezlynx.com", "token": "https://app.ezlynx.com/auth/connect/token"},
    "uat": {"host": "app.uatezlynx.com", "token": "https://app.uatezlynx.com/auth/connect/token"},
}
DOCUMENT_PATHS = ("/DocumentApi", "/DocumentApi/")


class DiscussionRouteRefused(EzlynxApiConfigurationError):
    """The Discussion API route is not the approved one, or cannot be established. Nothing was called."""


def resolve_route(environ: dict[str, str] | None = None) -> str:
    env = dict(os.environ if environ is None else environ)
    robie_env = str(env.get("ROBIE_ENV") or "").strip().upper()
    declared = str(env.get(ROUTE_ENV) or "").strip().lower()
    if declared and declared not in ROUTES:
        raise DiscussionRouteRefused(f"{ROUTE_ENV} must be one of {ROUTES}; refusing {declared!r}")
    if robie_env in PRODUCTION_ENV_NAMES:
        if declared and declared != "live":
            raise DiscussionRouteRefused("PRODUCTION may only use the live route")
        return "live"
    if robie_env != TEST_ENV_NAME:
        raise DiscussionRouteRefused("ROBIE_ENV must be TEST or PRODUCTION")
    if not declared:
        raise DiscussionRouteRefused(f"TEST must declare its Discussion API route in {ROUTE_ENV} (uat or live); there is no default")
    from .ezlynx_api_only_writes import DISCUSSION_API_ENV, LIVE_DISCUSSION_API, discussion_api_target

    try:
        switch_live = discussion_api_target(env) == LIVE_DISCUSSION_API
    except RuntimeError as exc:
        raise DiscussionRouteRefused(str(exc)) from exc
    if (declared == "live") != switch_live:
        raise DiscussionRouteRefused(
            f"{ROUTE_ENV}={declared} conflicts with {DISCUSSION_API_ENV}; they must agree. Nothing was read.")
    return declared


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # a 3xx is an error, never followed
        return None


def no_redirect_urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int,
                        _allow_insecure_test_origin: bool = False):
    """urlopen that refuses non-HTTPS URLs and never follows a redirect (so a token cannot be forwarded)."""
    parsed = urlparse(url)
    local_test = _allow_insecure_test_origin and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    if parsed.scheme != "https" and not local_test:
        raise DiscussionRouteRefused("only HTTPS requests are allowed")
    opener = request.build_opener(_NoRedirect)
    req = request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    return opener.open(req, timeout=timeout)


def _static_headers(url: str) -> dict[str, str]:
    """No browser session, no cookies, no portal headers: identity and content type only."""
    from .ezlynx_discussions import _CHROME_USER_AGENT

    return {"User-Agent": _CHROME_USER_AGENT, "Accept": "application/json"}


def _check_pair(route: str, api: Any) -> str:
    approved = APPROVED[route]
    token = str(api.token_endpoint or "")
    base = urlparse(str(api.document_base_url or ""))
    if token != approved["token"]:
        raise DiscussionRouteRefused(f"the {route} route's authentication endpoint is not the approved HTTPS endpoint; refusing")
    if (base.scheme != "https" or base.netloc != approved["host"] or base.path not in DOCUMENT_PATHS
            or base.params or base.query or base.fragment):
        raise DiscussionRouteRefused(f"the {route} route's API origin is not the approved HTTPS origin; refusing")
    return approved["host"]


def _guarded(route_host: str, token_url: str, inner: Any) -> Any:
    def send(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int) -> Any:
        parsed = urlparse(str(url))
        ok = (parsed.scheme == "https" and parsed.netloc == route_host and not parsed.fragment
              and (str(url) == token_url or parsed.path.startswith("/DiscussionApi/")))
        if not ok:
            raise DiscussionRouteRefused("request refused: not the approved HTTPS origin")
        return inner(url, data=data, headers=headers, timeout=timeout)

    return send


def build_task_discussion_client(*, accessor: Any = None, urlopen: Any = None, browser_session: bool = False) -> Any:
    """``browser_session`` is False by default: the client never reads or attaches Chrome cookies. It is
    True only where a caller explicitly keeps the legacy behaviour (the intake and canary wrappers); it is
    recorded in ``route_record`` and the read-only inspection and lookup refuse a client with it on."""
    from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig

    route = resolve_route()
    secret_env = "ROBIE_EZLYNX_API_PROD_SECRET" if route == "live" else "ROBIE_EZLYNX_API_UAT_SECRET"
    try:
        api = load_ezlynx_api_config(environment="PRODUCTION" if route == "live" else TEST_ENV_NAME, accessor=accessor)
    except DiscussionRouteRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - reason only, never secret text; no fallback to the other secret
        raise DiscussionRouteRefused(f"the {route} route's secret is unavailable ({type(exc).__name__}); not falling back") from exc
    host = _check_pair(route, api)
    client = DiscussionApiClient(
        DiscussionApiConfig(
            discussion_base_url=f"https://{host}/DiscussionApi/", token_endpoint=str(api.token_endpoint),
            client_id=str(api.client_id), client_secret=str(api.client_secret), username=str(api.username),
            integration_group_id=str(api.integration_group_id), scope="DiscussionApi openid", password=""),
        urlopen=_guarded(host, APPROVED[route]["token"], urlopen or no_redirect_urlopen),
        session_headers=None if browser_session else _static_headers)
    client.route_record = {"route": route, "host": host, "secret_ref": str(os.environ.get(secret_env) or ""),
                           "password_grant": False, "browser_cookies": bool(browser_session)}
    return client
