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
- BROWSER DISCOVERY 2026-09-27 (read-only, observed live): Additional
  Interests live on the classic policy summary page
  /applicantportal/policy/{policyId}/summary/index, SERVER-RENDERED in
  the initial HTML — no JSON/XHR endpoint exists (no network log
  available, but the section is in the DOM at document load with no
  lazy-load). Section = <h5>Additional Interests</h5> + plain <table>.
  Headers: "Name" | "Address" | "City / State / Zip" | "Type" |
  "Account #" | "Location #" | "Building #". There is NO "Loan Number"
  field — the loan/account field is labeled "Account #" (renders "---"
  when empty). Observed row: Valley National Bank Its Succ &/Or
  Assigns Atim, PO Box 3409, Coppell TX 75019-6403, Type=Mortgagee,
  Account #=---, Location #=2, Building #=1 (BOP #4714413).
  Policies without interests (e.g. E&O #0296536326) show NO section.
  -> build_browser_interests_reader() implements this read
  deterministically: policy number -> policyId (OAuth search) ->
  summary HTML (portal session, injected fetch_html_fn) -> parse.

Nothing here touches the network at import time. The OAuth client is
imported lazily so unit tests and dry runs never need credentials.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Callable

from .mortgagee_enrichment import EnrichmentPorts


POLICY_SUMMARY_URL = (
    "https://app.ezlynx.com/applicantportal/policy/{policy_id}/summary/index"
)

# Observed 2026-09-27 on the classic policy summary page. Keys are the
# normalized <th> labels; values are the entry-dict keys consumed by
# mortgagee_enrichment._mortgages_from_entries.
_HEADER_MAP = {
    "name": "lender_name",
    "address": "address",
    "city / state / zip": "city_state_zip",
    "type": "interest_type",
    "account #": "loan_number",
    "location #": "location_number",
    "building #": "building_number",
}

_EMPTY_CELL = {"---", ""}


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


class _AdditionalInterestsParser(HTMLParser):
    """Parse the classic policy summary's Additional Interests table.

    Observed 2026-09-27: <h5>Additional Interests</h5> followed by a plain
    <table> whose <th> row carries the field labels. Only the first table
    after the heading is read. stdlib only — no bs4 dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h5 = False
        self._h5_text = ""
        self._section_found = False
        self._in_table = False
        self._table_done = False
        self._in_th = False
        self._in_td = False
        self._cell = ""
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag == "h5" and not self._section_found:
            self._in_h5 = True
            self._h5_text = ""
        elif tag == "table" and self._section_found and not self._table_done:
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag == "th" and self._in_table:
            self._in_th = True
            self._cell = ""
        elif tag == "td" and self._in_table:
            self._in_td = True
            self._cell = ""

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h5" and self._in_h5:
            self._in_h5 = False
            if self._h5_text.strip().lower() == "additional interests":
                self._section_found = True
        elif tag == "table" and self._in_table:
            self._in_table = False
            self._table_done = True
        elif tag == "th" and self._in_th:
            self._in_th = False
            self.headers.append(self._cell.strip())
        elif tag == "td" and self._in_td:
            self._in_td = False
            self._row.append(self._cell.strip())
        elif tag == "tr" and self._in_table and self._row:
            # Data rows use <td>; the header row was captured via <th>.
            self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_h5:
            self._h5_text += data
        elif self._in_th or self._in_td:
            self._cell += data


def parse_additional_interests_html(html: str) -> list[dict[str, Any]]:
    """Extract Additional Interests entries from a policy summary page.

    Pure function of the HTML — deterministic and unit-testable. Returns
    [] when the page has no Additional Interests section (observed on
    policies with no interests). "---" cells become "".
    """
    parser = _AdditionalInterestsParser()
    parser.feed(html or "")
    if not parser.headers:
        return []
    key_by_col = [_HEADER_MAP.get(h.strip().lower()) for h in parser.headers]
    entries: list[dict[str, Any]] = []
    for row in parser.rows:
        entry: dict[str, Any] = {}
        for col, cell in enumerate(row):
            if col >= len(key_by_col):
                break
            key = key_by_col[col]
            if key is None:
                continue
            entry[key] = "" if cell.strip() in _EMPTY_CELL else cell.strip()
        if entry:
            entries.append(entry)
    return entries


def build_browser_interests_reader(
    policy_search_fn: Callable[[str], dict[str, Any] | None],
    fetch_html_fn: Callable[[str], str],
) -> Callable[[str], list[dict[str, Any]]]:
    """Build the deterministic Additional Interests reader.

    Returned callable takes a POLICY NUMBER (not applicant id — the
    summary URL needs the internal policyId, which only the policy
    search yields):

        policy number -> policy_search_fn -> policy_id
                      -> fetch_html_fn(summary url) -> parse -> entries

    Only rows with Type == "Mortgagee" (case-insensitive) are returned;
    other interest types (additional insured, certificate holder, ...)
    are not mortgagees and would corrupt the worker's lender checks.

    fetch_html_fn(url) -> HTML string is injected by the runtime: it
    performs the GET inside a portal-session browser. This module never
    drives a browser itself. Lookup failures raise (the enrichment
    layer converts them to HOLD with the reason), they are never
    returned as empty results.
    """

    def _read(policy_number: str) -> list[dict[str, Any]]:
        number = str(policy_number or "").strip()
        if not number:
            raise ValueError("policy number is required")
        found = policy_search_fn(number) or {}
        policy_id = str(found.get("policy_id") or "").strip()
        if not policy_id:
            raise RuntimeError(
                f"policy {number} did not resolve to an EZLynx policy id — "
                "cannot read additional interests")
        html = fetch_html_fn(POLICY_SUMMARY_URL.format(policy_id=policy_id))
        entries = parse_additional_interests_html(html)
        return [e for e in entries
                if str(e.get("interest_type") or "").strip().lower()
                == "mortgagee"]

    return _read


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
    fetch_html_fn: Callable[[str], str] | None = None,
) -> EnrichmentPorts:
    """Assemble the live EnrichmentPorts for the mortgagee worker.

    policy_api_client: a pre-built OAuth client exposing
        search_policy_by_number (tests inject a fake; the runtime may
        pass the deployed client). None -> lazy-load at call time.
    fetch_html_fn: callable(url) -> HTML string, run by the runtime
        inside a portal-session browser. When given, the deterministic
        Additional Interests reader is built from it (policy number ->
        policyId -> summary HTML -> parse); this is the designated live
        path until an API exposes additional interests.
    browser_interests_reader: legacy override — callable(policy_number)
        -> list of {"lender_name", "loan_number", ...}. Used only when
        fetch_html_fn is not given.
    """
    def _policy_search(policy_number: str) -> dict[str, Any] | None:
        return policy_search_via_oauth(policy_number, client=policy_api_client)

    reader = browser_interests_reader
    if fetch_html_fn is not None:
        reader = build_browser_interests_reader(_policy_search, fetch_html_fn)

    return EnrichmentPorts(
        policy_search_fn=_policy_search,
        additional_interests_fn=None,  # no working API path (see module docstring)
        browser_interests_fn=reader,
        lender_directory_fn=None,
    )
