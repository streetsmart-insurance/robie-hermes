#!/usr/bin/env python3
"""Governed single-shot canary policy creation and carrier read-back against live EZLynx.

Strictly bounded to:
  - Applicant 220250093 (ROBIE Test LLC) only.
  - Policy numbers matching TEST-CANARY-YYYYMMDD-NN.
  - Exactly ONE create call per run (no retries, no type permutations).
  - Pre-checks enumerable LOB codes and writing company rosters (read-only).
  - Captures raw request headers, payload, status, response headers, and raw response body.
  - On 200, reads back carrier value literally via PolicyApi search.
  - Supports --read-back-only to inspect created policy without issuing a create call.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.parse
from typing import Any

CALL_TIMEOUT = 30
CANARY_APPLICANT_ID = "220250093"
POLICY_NUMBER_PATTERN = re.compile(r"^TEST-CANARY-[0-9]{8}-[0-9]{2}$")

REQUIRED_SECRET_FIELDS = (
    "client_id",
    "client_secret",
    "username",
    "integration_group_id",
    "token_endpoint",
    "document_base_url",
    "scope",
)


def load_secret(secret_resource: str) -> dict[str, Any]:
    parts = secret_resource.split("/")
    project, name = parts[1], parts[3]
    proc = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            name,
            "--project",
            project,
        ],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gcloud secret access failed: {proc.stderr[:160]}")
    cfg = json.loads(proc.stdout.strip())
    missing = [f for f in REQUIRED_SECRET_FIELDS if not cfg.get(f)]
    if missing:
        raise RuntimeError(f"secret missing fields: {missing}")
    return cfg


def get_token(cfg: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(cfg["token_endpoint"])
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
    try:
        body = urllib.parse.urlencode({
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "vendor_data_access",
            "scope": cfg["scope"],
            "username": cfg["username"],
            "integration_group_id": cfg["integration_group_id"],
        })
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"token request returned HTTP {resp.status}")
    payload = json.loads(raw.decode("utf-8"))
    token = payload.get("access_token") or payload.get("token")
    if not token:
        raise RuntimeError("token response carried no access_token")
    return str(token)


def origin_of(cfg: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(cfg.get("document_base_url") or cfg["token_endpoint"])
    return f"{parsed.scheme}://{parsed.netloc}"


def oauth_request(
    method: str,
    url: str,
    token: str,
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str, Any]:
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=CALL_TIMEOUT)
    req_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        req_headers.update(headers)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    try:
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        resp_headers = dict(resp.getheaders())
        raw_bytes = resp.read()
        raw_str = raw_bytes.decode("utf-8", errors="replace")
        status = resp.status
    finally:
        conn.close()

    parsed_json = None
    try:
        parsed_json = json.loads(raw_str)
    except Exception:
        pass

    return status, resp_headers, raw_str, parsed_json


def extract_policy_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("Policies", "policies", "Results", "results", "Items", "items", "Data", "data"):
            cand = payload.get(k)
            if isinstance(cand, list):
                return [r for r in cand if isinstance(r, dict)]
        return [payload]
    return []


def find_policy_row(payload: Any, policy_number: str) -> dict[str, Any] | None:
    for row in extract_policy_rows(payload):
        r_num = str(row.get("PolicyNumber") or row.get("policyNumber") or "")
        if r_num.strip().casefold() == policy_number.strip().casefold():
            return row
    return None


def extract_carrier_info(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"carrier_literal": None, "raw_carrier_fields": {}}
    carrier_val = None
    for k in ("Carrier", "carrier", "CarrierId", "carrierId", "CarrierID", "CarrierName", "carrierName"):
        if k in row and row[k] is not None:
            carrier_val = str(row[k])
            break
    raw_carrier = {
        k: v for k, v in row.items()
        if any(t in k.casefold() for t in ("carrier", "company", "master", "writing"))
    }
    return {
        "carrier_literal": carrier_val,
        "raw_carrier_fields": raw_carrier,
    }


def execute_canary_create(
    origin: str,
    token: str,
    applicant_id: str,
    policy_number: str,
    master_company: int = 13585,
    writing_company: str = "10048",
    rating_state: str = "NJ",
    effective_date: str = "2026-10-02T00:00:00",
    expiration_date: str = "2027-10-02T00:00:00",
    premium: float = 1.00,
    read_back_only: bool = False,
) -> dict[str, Any]:
    """Execute pre-checks, exactly one create call, and post-create read-back."""
    if str(applicant_id).strip() != CANARY_APPLICANT_ID:
        raise ValueError(f"applicant_id must be {CANARY_APPLICANT_ID}, got {applicant_id}")
    if not POLICY_NUMBER_PATTERN.match(policy_number):
        raise ValueError(f"policy_number must match {POLICY_NUMBER_PATTERN.pattern}, got {policy_number}")

    search_url = f"{origin}/PolicyApi/policy/v1/search?{urllib.parse.urlencode({'PolicyNumber': policy_number})}"

    if read_back_only:
        rb_status, _, rb_raw, rb_json = oauth_request("GET", search_url, token)
        matched_row = find_policy_row(rb_json, policy_number) if rb_status == 200 else None
        c_info = extract_carrier_info(matched_row)
        return {
            "script": "canary_create_policy.py",
            "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "applicant_id": applicant_id,
            "policy_number": policy_number,
            "mode": "read_back_only",
            "create_attempt_count": 0,
            "read_back": {
                "status": rb_status,
                "found": matched_row is not None,
                "carrier_literal": c_info["carrier_literal"],
                "raw_carrier_fields": c_info["raw_carrier_fields"],
                "matched_row": matched_row,
                "raw_response_snippet": rb_raw[:1000] if not matched_row else None,
            },
            "verdict": "READ_BACK_SUCCESS" if matched_row else "READ_BACK_NOT_FOUND",
        }

    report: dict[str, Any] = {
        "script": "canary_create_policy.py",
        "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "applicant_id": applicant_id,
        "policy_number": policy_number,
        "create_attempt_count": 0,
        "enumerable_lob_codes": None,
        "enumerable_writing_companies": None,
        "pre_create_search": None,
        "create_call": None,
        "read_back": None,
        "verdict": "UNVERIFIED",
    }

    # Step 1: Query enumerable LOB codes (Read-only)
    lob_url = f"{origin}/PolicyApi/policy/v1/lob-codes"
    lob_status, _, lob_raw, lob_json = oauth_request("GET", lob_url, token)
    report["enumerable_lob_codes"] = {
        "status": lob_status,
        "count": len(lob_json) if isinstance(lob_json, list) else 0,
        "items": lob_json if isinstance(lob_json, list) else lob_raw[:500],
    }

    # Select LOB code: inspect enumerable list if available, default to "HOME"
    selected_lob = "HOME"
    if isinstance(lob_json, list):
        for item in lob_json:
            if isinstance(item, dict):
                code = str(item.get("code", "")).strip()
                name = str(item.get("name", "")).strip().casefold()
                if "homeowner" in name or code.casefold() in ("home", "ho"):
                    selected_lob = code
                    break

    # Step 2: Query enumerable writing companies for masterCompany (Read-only)
    wc_url = f"{origin}/PolicyApi/policy/v1/writing-companies/{master_company}"
    wc_status, _, wc_raw, wc_json = oauth_request("GET", wc_url, token)
    report["enumerable_writing_companies"] = {
        "status": wc_status,
        "master_company": master_company,
        "items": wc_json if isinstance(wc_json, list) else wc_raw[:500],
    }

    # Step 3: Pre-check duplicate via PolicyApi search (Read-only)
    s_status, _, s_raw, s_json = oauth_request("GET", search_url, token)
    pre_match = find_policy_row(s_json, policy_number) if s_status == 200 else None
    pre_c_info = extract_carrier_info(pre_match)
    report["pre_create_search"] = {
        "status": s_status,
        "exists": pre_match is not None,
        "carrier_literal": pre_c_info["carrier_literal"],
        "raw_carrier_fields": pre_c_info["raw_carrier_fields"],
        "matched_row": pre_match,
    }
    if pre_match is not None:
        report["verdict"] = "REFUSED_ALREADY_EXISTS"
        return report

    # Step 4: THE CREATE CALL (EXACTLY ONE ATTEMPT)
    create_url = f"{origin}/PolicyApi/account/{applicant_id}/policy/v1/create"
    payload = {
        "accountId": int(applicant_id),
        "policyNumber": policy_number,
        "writingCompany": str(writing_company),
        "lob": selected_lob,
        "effectiveDate": effective_date,
        "expirationDate": expiration_date,
        "masterCompany": int(master_company),
        "writtenPremium": float(premium),
        "ratingState": rating_state,
        "transactionType": "NBS",
        "acordXml": "",
    }
    payload_str = json.dumps(payload)

    report["create_attempt_count"] = 1
    create_headers = {"Content-Type": "application/json"}
    c_status, c_headers, c_raw, c_json = oauth_request(
        "POST", create_url, token, body=payload_str, headers=create_headers
    )

    report["create_call"] = {
        "url": create_url,
        "method": "POST",
        "request_headers": {
            "Authorization": "Bearer ***",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        },
        "request_payload": payload,
        "response_status": c_status,
        "response_headers": c_headers,
        "response_body_raw": c_raw,
        "response_body_json": c_json,
    }

    # If create failed (non-200), STOP IMMEDIATELY. No retry, no permutations.
    if c_status != 200:
        report["verdict"] = "CREATE_FAILED"
        return report

    # Create returned 200!
    created_policy_id = str(c_json) if c_json is not None else c_raw.strip().strip('"')
    report["created_policy_id"] = created_policy_id

    # Step 5: Read-Back via PolicyApi Search to report carrier literally
    rb_status, _, rb_raw, rb_json = oauth_request("GET", search_url, token)
    matched_row = find_policy_row(rb_json, policy_number) if rb_status == 200 else None
    c_info = extract_carrier_info(matched_row)

    report["read_back"] = {
        "status": rb_status,
        "found": matched_row is not None,
        "carrier_literal": c_info["carrier_literal"],
        "raw_carrier_fields": c_info["raw_carrier_fields"],
        "matched_row": matched_row,
        "raw_response_snippet": rb_raw[:1000] if not matched_row else None,
    }
    report["verdict"] = "SUCCESS"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--applicant-id",
        required=True,
        help="EZLynx applicant ID. Locked to 220250093.",
    )
    parser.add_argument(
        "--policy-number",
        required=True,
        help="Policy number matching TEST-CANARY-YYYYMMDD-NN",
    )
    parser.add_argument(
        "--read-back-only",
        action="store_true",
        help="Read back policy and carrier without issuing create call",
    )
    parser.add_argument(
        "--secret",
        default="projects/751771086524/secrets/ezlynx-api-prod/versions/latest",
        help="Secret Manager reference",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to output JSON file",
    )
    args = parser.parse_args()

    try:
        cfg = load_secret(args.secret)
        token = get_token(cfg)
        origin = origin_of(cfg)
        result = execute_canary_create(
            origin=origin,
            token=token,
            applicant_id=args.applicant_id,
            policy_number=args.policy_number,
            read_back_only=args.read_back_only,
        )
    except Exception as exc:
        result = {
            "script": "canary_create_policy.py",
            "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "applicant_id": args.applicant_id,
            "policy_number": args.policy_number,
            "verdict": "EXCEPTION",
            "error": str(exc),
        }

    output_str = json.dumps(result, indent=2)
    print(output_str)

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_str)

    return 0 if result.get("verdict") in ("SUCCESS", "CREATE_FAILED", "READ_BACK_SUCCESS", "REFUSED_ALREADY_EXISTS") else 1


if __name__ == "__main__":
    sys.exit(main())
