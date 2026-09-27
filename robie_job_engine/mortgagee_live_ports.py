"""Live adapter wiring for mortgagee enrichment ports.

Builds the read-only adapters that plug into
mortgagee_enrichment.EnrichmentPorts:

    policy_search_fn      -> OAuth PolicyApi search?PolicyNumber=
                             (LIVE-PROVEN 2026-09-27, hermes-poc-01)
    additional_interests_fn -> NOT WIRED — no working API path exists
                             (see probe notes below). Left as None so
                             enrichment fail-closes with HOLD.
    browser_interests_fn  -> injected by the runtime (the designated
                             live path for Additional Interests reads).

Probe notes, 2026-09-27 (all read-only, hermes-poc-01 + sandbox):
- OAuth GET /PolicyApi/policy/v1/search?PolicyNumber=0296536326 -> 200,
  rows carry accountId/policyId. policy_search_fn is built on this.
- OAuth GET /PolicyApi/policy/v1/{id}/additional-interests -> 404.
- OAuth GET /PolicyApi/policy/v1/{id}/additionalinterests -> 404.
- OAuth GET /PolicyApi/policy/v1/{id} -> 200 but the record has NO
  mortgagee/additional-interest fields (38 keys checked).
- Classic API: authenticate 200, then data calls 401 "Passed in user is
  not the developer of app" (combo 1); alternate combo 400 at
  authenticate (combo 2). No Classic data path today.

Nothing here touches the network at import time. The OAuth client is
imported lazily so unit tests and dry runs never need credentials.
"""
from __future__ import annotations

from typing import Any, Callable

from .mortgagee_enrichment import EnrichmentPorts


def policy_search_via_oauth(policy_number: str,
                            client: Any = None) -> dict[str, Any] | None:
    """Read-only: policy number -> {"applicant_id": <EZLynx accountId>}.

    Uses the deployed OAuth PolicyApi client (search_policy_by_number).
    Returns None when the policy is not found or the client is unavailable
    — the enrichment layer treats that as HOLD, never as a guess.
    """
    number = str(policy_number or "").strip()
    if not number:
        return None
    api = client if client is not None else _load_oauth_client()
    if api is None:
        return None
    result = api.search_policy_by_number(number)
    data = result.get("data", result) if isinstance(result, dict) else {}
    rows = data.get("results", []) if isinstance(data, dict) else []
    if not rows:
        return None
    first = rows[0] if isinstance(rows[0], dict) else {}
    applicant_id = str(first.get("accountId") or "").strip()
    if not applicant_id:
        return None
    return {"applicant_id": applicant_id,
            "policy_id": str(first.get("policyId") or "").strip()}


def _load_oauth_client() -> Any | None:
    """Build the deployed OAuth client, or None when unavailable."""
    try:
        from . import ezlynx_api as _api  # lazy: not on every branch
    except ImportError:
        try:
            import ezlynx_api as _api  # noqa: F401
        except ImportError:
            return None
    try:
        return _api.EzlynxApiClient(_api.load_ezlynx_api_config())
    except Exception:
        return None


def build_live_ports(
    *,
    policy_api_client: Any = None,
    browser_interests_reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> EnrichmentPorts:
    """Assemble the live EnrichmentPorts for the mortgagee worker.

    policy_api_client: a pre-built OAuth client exposing
        search_policy_by_number (tests inject a fake; the runtime may
        pass the deployed client). None -> lazy-load at call time.
    browser_interests_reader: callable(applicant_id) -> list of
        {"lender_name", "loan_number", ...}. The designated live path
        for Additional Interests until an API exposes them.
    """
    def _policy_search(policy_number: str) -> dict[str, Any] | None:
        return policy_search_via_oauth(policy_number, client=policy_api_client)

    return EnrichmentPorts(
        policy_search_fn=_policy_search,
        additional_interests_fn=None,  # no working API path (see module docstring)
        browser_interests_fn=browser_interests_reader,
        lender_directory_fn=None,
    )
