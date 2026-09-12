#!/usr/bin/env python3
"""Execute a single governed canary policy create call and verify read-back.

Strict invariants:
1. Target applicant locked to 220250093 (ROBIE Test LLC). Any other account is refused.
2. Exactly ONE create call per execution. No retries, no type permutations.
3. If the create call fails (non-200), capture raw status, request body, and response body,
   STOP immediately, and report.
4. If the create call succeeds (200), read back via PolicyApi search, report the carrier
   literally, and record all matched attributes.
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

CANARY_APPLICANT_ID = "220250093"
POLICY_NUMBER_PATTERN = re.compile(r"^TEST-CANARY-[0-9]{8}-[0-9]{2}$")
CALL_TIMEOUT = 30  # seconds

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
    """Execute an HTTP request with Bearer auth and return (status, headers, raw_body, parsed_json_or_None)."""
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
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
) -> dict[str, Any]:
    """Execute pre-checks, exactly one create call, and post-create read-back."""
    if str(applicant_id).strip() != CANARY_APPLICANT_ID:
        raise ValueError(f"applicant_id must be {CANARY_APPLICANT_ID}, got {applicant_id}")
    if not POLICY_NUMBER_PATTERN.match(policy_number):
        raise ValueError(f"policy_number must match {POLICY_NUMBER_PATTERN.pattern}, got {policy_number}")

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
    search_url = f"{origin}/PolicyApi/policy/v1/search?{urllib.parse.urlencode({'PolicyNumber': policy_number})}"
    s_status, _, s_raw, s_json = oauth_request("GET", search_url, token)
    report["pre_create_search"] = {
        "status": s_status,
        "exists": False,
    }
    if s_status == 200 and isinstance(s_json, list) and len(s_json) > 0:
        report["pre_create_search"]["exists"] = True
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
    matched_row = None
    if rb_status == 200:
        candidates = rb_json if isinstance(rb_json, list) else []
        if isinstance(rb_json, dict) and "data" in rb_json:
            candidates = rb_json["data"]
        for row in candidates:
            if isinstance(row, dict):
                r_num = str(row.get("PolicyNumber") or row.get("policyNumber") or "")
                if r_num.strip().casefold() == policy_number.strip().casefold():
                    matched_row = row
                    break

    carrier_literal = None
    if matched_row:
        for k in ("Carrier", "carrier", "CarrierId", "carrierId", "CarrierID"):
            if k in matched_row:
                carrier_literal = str(matched_row[k])
                break

    report["read_back"] = {
        "status": rb_status,
        "found": matched_row is not None,
        "carrier_literal": carrier_literal,
        "matched_row": matched_row,
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

    return 0 if result.get("verdict") in ("SUCCESS", "CREATE_FAILED") else 1


if __name__ == "__main__":
    sys.exit(main())
