#!/usr/bin/env python3
"""Proof orchestrator: search-first PolicyApi create + FormEntry coverages by label.

Carlo 2026-09-12: full build+test today, Dusty orchestrates, Ralph codes.
Do not SSH. Do not deploy. Do not message Robie. Never create
TEST-HO-20260911-E01 by hand. Applicant 220250093 only.

Pipeline:
  1. Session: re-auth via Secret Manager if logged out (no human).
  2. API: search-first by policy number; create with the gold payload
     (writingCompany "10048" string, masterCompany 13585 int) only if absent.
  3. Browser: open the policy's FormEntry Coverages tab and fill by literal
     label (never the invented HO Coverage id selectors).

Every step records destination evidence. A step that cannot proceed stops
the run with a named marker — nothing is guessed, nothing is retried blindly.
"""
from __future__ import annotations

import datetime
from typing import Any

PROOF_APPLICANT_ID = "220250093"

EDIT_URL_TEMPLATE = (
    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/{applicant}/{policy_id}"
)


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_applicant_scope(applicant_id: str) -> None:
    if str(applicant_id).strip() != PROOF_APPLICANT_ID:
        raise ValueError(
            f"proof writes are scoped to applicant {PROOF_APPLICANT_ID} only; "
            f"got {applicant_id!r}"
        )


# Policy ID key variants seen in PolicyApi search/create responses.
# "PolicyID" (capital P, capital ID) is the variant that broke job 4dfee5f4:
# the pre-create search found TEST-HO-20260911-E01 (ALREADY_EXISTS) but the
# row carried the ID under "PolicyID", which the old 5-key list missed,
# producing the misleading "Create HTTP None" diagnostic for a create that
# was never attempted.
_POLICY_ID_KEYS = ("policyId", "policyID", "PolicyId", "PolicyID", "id", "policy_id")


def _extract_policy_id(row: Any) -> str | None:
    """Best-effort policy ID extraction from a PolicyApi row (any shape)."""
    if not isinstance(row, dict):
        return None
    for key in _POLICY_ID_KEYS:
        if row.get(key):
            return str(row[key])
    # Case-insensitive fallback: any key spelling "policyid".
    for key, value in row.items():
        if isinstance(key, str) and key.lower() == "policyid" and value:
            return str(value)
    return None


def ensure_session() -> dict[str, Any]:
    """Re-auth via Secret Manager when logged out. Returns the recovery report."""
    from .session_recovery import attempt_session_recovery

    return attempt_session_recovery()


def search_first_create(
    client: Any,
    *,
    applicant_id: str,
    policy_number: str,
    effective_date: str,
    expiration_date: str,
) -> dict[str, Any]:
    """Search-first PolicyApi create with the gold payload. Exactly one create max."""
    ensure_applicant_scope(applicant_id)
    report: dict[str, Any] = {
        "policy_number": policy_number,
        "applicant_id": str(applicant_id),
        "pre_create_search": None,
        "create": None,
        "read_back": None,
        "verdict": "UNVERIFIED",
        "policy_id": None,
    }
    search = client.search_policy_by_number(policy_number)
    rows = search.get("data") if isinstance(search, dict) else None
    matched = None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("policyNumber") or row.get("PolicyNumber") or "").strip() == policy_number:
                matched = row
                break
    elif isinstance(rows, dict):
        matched = rows
    report["pre_create_search"] = {"found": matched is not None, "row": matched}
    if matched is not None:
        report["verdict"] = "ALREADY_EXISTS"
        report["read_back"] = matched
        report["policy_id"] = _extract_policy_id(matched)
        return report

    created = client.create_policy(
        applicant_id=str(applicant_id),
        policy_number=policy_number,
        effective_date=effective_date,
        expiration_date=expiration_date,
    )
    # The create endpoint returns the new policy ID as a bare scalar
    # (string or number). Capture it for fallback if read-back lags.
    create_resp = created.get("response")
    create_policy_id = None
    if isinstance(create_resp, (str, int)):
        pid = str(create_resp).strip().strip('"')
        if pid and pid.lstrip('-').isdigit():
            create_policy_id = pid
    elif isinstance(create_resp, dict):
        create_policy_id = _extract_policy_id(create_resp)
    report["create"] = {
        "request_payload": created.get("request_payload"),
        "response": created.get("response"),
        "http_status": created.get("http_status"),
        "raw_body": created.get("raw_body"),
        "url": created.get("url"),
        "response_type": created.get("response_type"),
        "response_headers": created.get("response_headers"),
        "status_source": created.get("status_source"),
        "policy_id": create_policy_id,
    }
    # Read back through search to prove destination state.
    # Retry with delay: the new policy may not be searchable immediately
    # (eventual consistency). Up to 3 attempts, 3s apart.
    import time as _time
    rmatch = None
    for attempt in range(3):
        recheck = client.search_policy_by_number(policy_number)
        rrows = recheck.get("data") if isinstance(recheck, dict) else None
        if isinstance(rrows, list):
            for row in rrows:
                if isinstance(row, dict) and str(row.get("policyNumber") or row.get("PolicyNumber") or "").strip() == policy_number:
                    rmatch = row
                    break
        elif isinstance(rrows, dict):
            rmatch = rrows
        if rmatch is not None:
            break
        if attempt < 2:
            _time.sleep(3)
    report["read_back"] = rmatch
    if rmatch:
        report["verdict"] = "CREATED_AND_READ_BACK"
        report["policy_id"] = create_policy_id or _extract_policy_id(rmatch)
    elif create_policy_id:
        # Create succeeded and returned an ID, but search hasn't caught up yet.
        # The caller can proceed with the create ID.
        report["verdict"] = "CREATED_ID_FROM_CREATE_RESPONSE"
        report["policy_id"] = create_policy_id
    else:
        # Fail closed: no ID from create, no ID from read-back.
        # Include raw HTTP details for diagnosis.
        http_status = created.get("http_status")
        raw_body = created.get("raw_body") or ""
        body_preview = raw_body[:500] if len(raw_body) > 500 else raw_body
        resp_type = created.get("response_type")
        status_src = created.get("status_source")
        report["verdict"] = "CREATED_NO_POLICY_ID"
        report["no_id_diagnostic"] = (
            f"PolicyApi create returned HTTP {http_status} (via {status_src}, "
            f"response type {resp_type}) with body: {body_preview}. "
            f"No policy ID in create response, and read-back search found no matching policy."
        )
    return report


def open_formentry_coverages(page: Any, applicant_id: str, policy_id: str) -> dict[str, Any]:
    """Mint the FormEntry via Save & Continue Edit; watch validation + DOM.

    The door is the green Save & Continue Edit button on the Edit Policy
    header. The FormEntry URL is
    /applicantportal/Policy/{policyId}/FormEntry/Index/{formEntryId}.
    Polls the URL + DOM for 30s (not networkidle, not URL-only): captures
    pre/post-click validation state from the DOM.
    """
    from .ezlynx_account_nav import FORMENTRY_RE

    ensure_applicant_scope(applicant_id)
    report: dict[str, Any] = {
        "edit_url": EDIT_URL_TEMPLATE.format(applicant=applicant_id, policy_id=policy_id),
        "landed_url": None,
        "formentry_found": False,
        "validation": {},
    }
    page.goto(report["edit_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    report["landed_url"] = page.url
    if "/applicantportal/Policy/" not in page.url:
        report["error"] = "left /applicantportal/Policy/; stopped"
        return report

    # Pre-click: scan every open tab for an already-minted FormEntry.
    for tab in _all_tabs_sync(page):
        try:
            url = tab.url
        except Exception:  # noqa: BLE001
            continue
        if FORMENTRY_RE.search(url or ""):
            report["formentry_found"] = True
            report["formentry_url"] = url
            report["via"] = "already_open_tab"
            return report

    report["validation"]["pre_click"] = _validation_snapshot_sync(page)

    button = page.get_by_role("button", name="Save & Continue Edit")
    if button.count() == 0:
        report["error"] = "Save & Continue Edit button not found on Edit Policy header"
        return report
    button.first.click()

    for _ in range(30):
        page.wait_for_timeout(1000)
        url = page.url
        if FORMENTRY_RE.search(url or ""):
            report["formentry_found"] = True
            report["formentry_url"] = url
            report["via"] = "save_and_continue_edit"
            return report
        for tab in _all_tabs_sync(page):
            try:
                turl = tab.url
            except Exception:  # noqa: BLE001
                continue
            if FORMENTRY_RE.search(turl or ""):
                report["formentry_found"] = True
                report["formentry_url"] = turl
                report["via"] = "save_and_continue_edit_new_tab"
                return report

    report["validation"]["post_click"] = _validation_snapshot_sync(page)
    report["landed_url"] = page.url
    report["error"] = (
        "Save & Continue Edit clicked; no FormEntry URL after 30s. "
        "See validation snapshot for blocking errors."
    )
    return report


def _all_tabs_sync(page: Any) -> list[Any]:
    try:
        return list(page.context.pages)
    except Exception:  # noqa: BLE001
        return [page]


def _validation_snapshot_sync(page: Any) -> dict[str, Any]:
    js = r"""
    () => {
      const fieldErrors = Array.from(
        document.querySelectorAll(".field-validation-error, .validation-message, [data-valmsg-for]")
      ).map((el) => (el.innerText || "").trim()).filter(Boolean).slice(0, 20);
      const summary = Array.from(
        document.querySelectorAll(".validation-summary-errors")
      ).map((el) => (el.innerText || "").trim()).filter(Boolean).slice(0, 5);
      const ariaInvalid = Array.from(
        document.querySelectorAll("[aria-invalid='true']")
      ).map((el) => el.id || el.getAttribute("name") || el.tagName).slice(0, 20);
      return {field_errors: fieldErrors, summary_errors: summary, aria_invalid: ariaInvalid,
              url: location.href};
    }
    """
    try:
        return page.evaluate(js)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_proof(
    *,
    client: Any,
    applicant_id: str = PROOF_APPLICANT_ID,
    policy_number: str,
    effective_date: str,
    expiration_date: str,
    coverages: dict[str, str] | None = None,
    page: Any | None = None,
) -> dict[str, Any]:
    """Run the full proof pipeline. Browser steps only when `page` is given."""
    ensure_applicant_scope(applicant_id)
    report: dict[str, Any] = {
        "started_at": _utcnow(),
        "applicant_id": str(applicant_id),
        "policy_number": policy_number,
        "session": None,
        "api": None,
        "formentry": None,
        "verdict": "UNVERIFIED",
    }
    session = ensure_session()
    report["session"] = session
    if not session.get("recovered"):
        # Session may still be valid (preflight only blocks on proven logout);
        # the recovery report tells Dusty what happened. Continue only if the
        # browser already holds a session — the API path does not need it.
        report["session_note"] = (
            "recovery did not report success; API steps continue, "
            "browser steps require an authenticated page"
        )
    api_report = search_first_create(
        client,
        applicant_id=str(applicant_id),
        policy_number=policy_number,
        effective_date=effective_date,
        expiration_date=expiration_date,
    )
    report["api"] = api_report
    if page is not None and coverages:
        policy_id = None
        row = api_report.get("read_back") or {}
        for key in ("policyId", "policyID", "id", "PolicyId"):
            if row.get(key):
                policy_id = str(row[key])
                break
        if not policy_id:
            report["formentry"] = {"error": "no policy id from read-back; cannot open FormEntry"}
        else:
            nav = open_formentry_coverages(page, str(applicant_id), policy_id)
            report["formentry"] = nav
            if nav.get("formentry_found"):
                from .formentry_coverages import fill_coverages_by_label

                nav["coverage_fill"] = fill_coverages_by_label(page, coverages)
    report["finished_at"] = _utcnow()
    return report
