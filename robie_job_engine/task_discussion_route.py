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
- The task flow never uses the shared SSRobie username/password: the token request carries no
  password, so it is the vendor integration grant only. (The note tool's live route is separate.)
- The client carries a ``route_record`` (route, host, secret resource name, grant), never a secret value.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from .ezlynx_api import EzlynxApiConfigurationError, load_ezlynx_api_config
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env

ROUTE_ENV = "ROBIE_TASK_DISCUSSION_ROUTE"
ROUTES = ("uat", "live")


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


def build_task_discussion_client(*, accessor: Any = None) -> Any:
    from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig

    route = resolve_route()
    secret_env = "ROBIE_EZLYNX_API_PROD_SECRET" if route == "live" else "ROBIE_EZLYNX_API_UAT_SECRET"
    try:
        api = load_ezlynx_api_config(environment="PRODUCTION" if route == "live" else TEST_ENV_NAME, accessor=accessor)
    except DiscussionRouteRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - reason only, never secret text; no fallback to the other secret
        raise DiscussionRouteRefused(f"the {route} route's secret is unavailable ({type(exc).__name__}); not falling back") from exc
    host = (urlparse(str(api.token_endpoint)).hostname or "").casefold()
    doc_host = (urlparse(str(api.document_base_url)).hostname or "").casefold()
    is_uat = "uatezlynx" in host or "uatezlynx" in doc_host
    if (route == "uat") != is_uat:
        raise DiscussionRouteRefused(f"the {route} route's secret points at {host or 'an unknown host'}; refusing")
    parsed = urlparse(str(api.document_base_url or api.token_endpoint))
    client = DiscussionApiClient(DiscussionApiConfig(
        discussion_base_url=f"{parsed.scheme}://{parsed.netloc}/DiscussionApi/",
        token_endpoint=str(api.token_endpoint), client_id=str(api.client_id), client_secret=str(api.client_secret),
        username=str(api.username), integration_group_id=str(api.integration_group_id),
        scope="DiscussionApi openid", password=""))
    client.route_record = {"route": route, "host": host, "secret_ref": str(os.environ.get(secret_env) or ""),
                           "password_grant": False}
    return client
