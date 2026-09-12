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
        return report

    created = client.create_policy(
        applicant_id=str(applicant_id),
        policy_number=policy_number,
        effective_date=effective_date,
        expiration_date=expiration_date,
    )
    report["create"] = {
        "request_payload": created.get("request_payload"),
        "response": created.get("response"),
    }
    # Read back through search to prove destination state.
    recheck = client.search_policy_by_number(policy_number)
    rrows = recheck.get("data") if isinstance(recheck, dict) else None
    rmatch = None
    if isinstance(rrows, list):
        for row in rrows:
            if isinstance(row, dict) and str(row.get("policyNumber") or row.get("PolicyNumber") or "").strip() == policy_number:
                rmatch = row
                break
    elif isinstance(rrows, dict):
        rmatch = rrows
    report["read_back"] = rmatch
    report["verdict"] = "CREATED_AND_READ_BACK" if rmatch else "CREATED_NO_READ_BACK"
    return report


def open_formentry_coverages(page: Any, applicant_id: str, policy_id: str) -> dict[str, Any]:
    """Land on the policy's FormEntry Coverages tab. Reports the landed URL."""
    from .ezlynx_account_nav import FORMENTRY_RE

    ensure_applicant_scope(applicant_id)
    report: dict[str, Any] = {
        "edit_url": EDIT_URL_TEMPLATE.format(applicant=applicant_id, policy_id=policy_id),
        "landed_url": None,
        "formentry_found": False,
        "coverages_tab": None,
    }
    page.goto(report["edit_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    report["landed_url"] = page.url
    if "/applicantportal/Policy/" not in page.url:
        report["error"] = "left /applicantportal/Policy/; stopped"
        return report

    # Scan every open tab for a FormEntry URL before clicking anything.
    try:
        contexts = page.context.browser.contexts if hasattr(page.context, "browser") else []
    except Exception:  # noqa: BLE001
        contexts = []
    tabs = []
    for ctx in contexts or [page.context]:
        try:
            tabs.extend(ctx.pages)
        except Exception:  # noqa: BLE001
            continue
    for tab in tabs:
        try:
            url = tab.url
        except Exception:  # noqa: BLE001
            continue
        if FORMENTRY_RE.search(url or ""):
            report["formentry_found"] = True
            report["formentry_url"] = url
            try:
                tab.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            break
    return report


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
