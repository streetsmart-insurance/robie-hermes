#!/usr/bin/env python3
"""Acceptance verifier for the ROBIE email policy-setup path.

Read-only by construction. Given an applicant ID, a policy number, and a
job ID, it re-reads destination state from primary sources and reports
what is true — never what the worker claimed.

Reads performed (all from hermes-poc-01, all GETs, all with timeouts):
  1. PolicyApi policy search by number (OAuth):
       GET {origin}/PolicyApi/policy/v1/search?PolicyNumber=
     Reports whether the named policy exists and its field values.
  2. Applicant policy list attempt (OAuth):
       GET {origin}/PolicyApi/policy/v1/search?ApplicantId=
     If the endpoint answers, every policy on the applicant is listed
     (number, status, carrier, effective, expiration) with a total count.
     If it does not answer, the field is reported UNCHECKED with the
     reason — the verifier never invents a list.
  3. Document Library (classic REST):
       GET {classic}/api/documentlibrary/list/{applicant}/1/200/0
     Reports every document row seen (name, description, policy number,
     policy id).
  4. jobs.db (SQLite, read-only, query_only):
     The job's own rows for the job ID passed as --job-id (never
     hardcoded): the jobs row, checkpoints, playwright_exec calls, and
     verification_evidence.

Output is one JSON object on stdout with an explicit "not_checked" list
naming every field that could not be read and why.

Never prints secret values. Never writes anything.

Usage:
  sudo python3 verify_acceptance.py \
      --applicant-id 220250093 \
      --policy-number TEST-HO-20260911-E01 \
      --job-id 1eeda98e-eedb-4758-8867-e9ac6116ac57 \
      --db-path /opt/streetsmart-hermes/robie-job-engine/data/jobs.db \
      --secret projects/751771086524/secrets/ezlynx-api-prod/versions/latest
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

CALL_TIMEOUT = 30  # seconds for every network operation
DOC_PAGE_SIZE = 200

# Markers only — fixed labels, never excerpts of tool output.
MARKERS = (
    "NEEDS_SKILL", "NEEDS_CLARIFICATION", "PLAYWRIGHT_BLOCKED",
    "EZLYNX_WRITE_SCOPE_REFUSED", "ROBIE_OUTCOME_UNKNOWN",
    "TimeoutError", "ModuleNotFoundError", "PermissionError",
    "ImportError", "AUTH_REQUIRED", "RESOURCE_EXHAUSTED",
    "max_iterations", "tool_calls", "finish_reason", "STOP",
    "MALFORMED_FUNCTION_CALL", "MAX_TOKENS",
)

REQUIRED_SECRET_FIELDS = (
    "client_id", "client_secret", "username", "integration_group_id",
    "token_endpoint", "document_base_url", "scope",
)
CLASSIC_TOKEN_KEYS = ("ez_token", "EZToken")
CLASSIC_SECRET_KEYS = ("ez_app_secret", "EZAppSecret")
CLASSIC_USER_KEYS = ("account_username", "AccountUsername")
CLASSIC_BASE_KEYS = ("classic_base_url", "classic_document_base_url")

DOCUMENT_RECORD_KEYS = (
    "Records", "records", "DocumentList", "Documents", "documents",
    "Items", "items", "Data", "data",
)
POLICY_RECORD_KEYS = (
    "Policies", "policies", "Results", "results", "Items", "items", "Data",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_markers(value: object) -> list[str]:
    text = str(value or "")
    return [m for m in MARKERS if m.casefold() in text.casefold()]


def first(mapping: dict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


# ---------------------------------------------------------------------------
# Secret Manager + OAuth
# ---------------------------------------------------------------------------

def load_secret(secret_resource: str) -> dict:
    parts = secret_resource.split("/")
    project, name = parts[1], parts[3]
    proc = subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         "--secret", name, "--project", project],
        capture_output=True, text=True, timeout=CALL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gcloud secret access failed: {proc.stderr[:160]}")
    cfg = json.loads(proc.stdout.strip())
    missing = [f for f in REQUIRED_SECRET_FIELDS if not cfg.get(f)]
    if missing:
        raise RuntimeError(f"secret missing fields: {missing}")
    return cfg


def get_token(cfg: dict) -> str:
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
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
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


def origin_of(cfg: dict) -> str:
    parsed = urllib.parse.urlparse(cfg["document_base_url"] or cfg["token_endpoint"])
    return f"{parsed.scheme}://{parsed.netloc}"


def oauth_get(url: str, token: str) -> tuple[int, object]:
    """Return (status, parsed JSON or None). Never raises on HTTP errors."""
    parsed = urllib.parse.urlparse(url)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    finally:
        conn.close()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, None


# ---------------------------------------------------------------------------
# Policy reads
# ---------------------------------------------------------------------------

def extract_policy_rows(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in POLICY_RECORD_KEYS:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [r for r in candidate if isinstance(r, dict)]
        return [payload]
    return []


def policy_fields(row: dict) -> dict:
    return {
        "policy_number": first(row, "PolicyNumber", "policyNumber", "policy_number"),
        "policy_id": first(row, "PolicyId", "policyId", "policy_id", "ID", "Id"),
        "status": first(row, "Status", "status", "PolicyStatus", "policyStatus"),
        "carrier": first(row, "CarrierName", "carrierName", "Carrier", "carrier",
                         "WritingCompany", "writingCompany"),
        "effective": first(row, "EffectiveDate", "effectiveDate", "effective",
                            "Effective", "PolicyEffectiveDate"),
        "expiration": first(row, "ExpirationDate", "expirationDate", "expiration",
                             "Expiration", "PolicyExpirationDate"),
        "applicant_id": first(row, "ApplicantId", "applicantId", "applicant_id",
                               "AccountId", "accountId"),
    }


def search_policy_by_number(origin: str, token: str, policy_number: str) -> dict:
    url = origin + "/PolicyApi/policy/v1/search?" + urllib.parse.urlencode(
        {"PolicyNumber": policy_number})
    status, payload = oauth_get(url, token)
    report: dict = {
        "read_method": "PolicyApi/policy/v1/search?PolicyNumber=",
        "http_status": status,
        "found": False,
        "matches": [],
    }
    if status != 200 or payload is None:
        report["error"] = f"search returned HTTP {status}"
        return report
    for row in extract_policy_rows(payload):
        if str(first(row, "PolicyNumber", "policyNumber", "policy_number")
               ).strip().casefold() == policy_number.strip().casefold():
            report["matches"].append(policy_fields(row))
    report["found"] = bool(report["matches"])
    return report


def list_policies_on_applicant(origin: str, token: str, applicant_id: str,
                              not_checked: list) -> dict:
    """Try applicant-scoped list endpoints; report exactly what answered."""
    report: dict = {"read_method": None, "http_status": None,
                    "policies": [], "total_count": 0}
    attempts = []
    # Candidate endpoints, all read-only GETs. The account-scoped candidates
    # are tried FIRST: they are the only ones that could return a truly
    # applicant-scoped list. The search?ApplicantId= variants are kept as
    # fallback because they return rows (unfiltered). The first candidate
    # that returns policy rows wins; every returned row's own applicant_id
    # is then checked, so an endpoint that ignores the applicant parameter
    # can never be mistaken for an applicant-scoped list.
    candidates = [
        ("PolicyApi/account/{id}/policy/v1/search",
         origin + f"/PolicyApi/account/{applicant_id}/policy/v1/search"),
        ("PolicyApi/account/{id}/policy/v1/list",
         origin + f"/PolicyApi/account/{applicant_id}/policy/v1/list"),
        ("PolicyApi/account/{id}/policies",
         origin + f"/PolicyApi/account/{applicant_id}/policies"),
        ("PolicyApi/policy/v1/search?ApplicantId=",
         origin + "/PolicyApi/policy/v1/search?" + urllib.parse.urlencode(
             {"ApplicantId": applicant_id})),
        ("PolicyApi/policy/v1/search?ApplicantID=",
         origin + "/PolicyApi/policy/v1/search?" + urllib.parse.urlencode(
             {"ApplicantID": applicant_id})),
    ]
    for label, url in candidates:
        status, payload = oauth_get(url, token)
        attempts.append({"endpoint": label, "http_status": status,
                         "json": payload is not None})
        if status == 200 and payload is not None:
            rows = extract_policy_rows(payload)
            if rows:
                policies = [policy_fields(r) for r in rows]
                matching = [p for p in policies
                            if str(p.get("applicant_id")) == str(applicant_id)]
                report["read_method"] = label
                report["http_status"] = status
                report["policies"] = policies
                report["total_count"] = len(policies)
                # The endpoint may ignore the applicant parameter and return a
                # global list. Never claim applicant scoping without checking
                # every returned row's own applicant_id.
                report["applicant_filter_honored"] = (
                    len(matching) == len(policies) and len(policies) > 0)
                report["policies_matching_applicant"] = len(matching)
                # Always record which endpoints were tried, even when one
                # won — the probe history is evidence either way.
                report["attempts"] = attempts
                if not report["applicant_filter_honored"]:
                    not_checked.append(
                        "policy list by applicant: the PolicyApi search "
                        "endpoint ignored the applicant parameter and "
                        "returned policies for other applicants; only "
                        f"{len(matching)} of {len(policies)} rows match "
                        f"applicant {applicant_id}")
                return report
    report["attempts"] = attempts
    not_checked.append(
        "policy list by applicant: no applicant-scoped PolicyApi endpoint "
        f"returned policy rows (attempts: {attempts})")
    return report


# ---------------------------------------------------------------------------
# Document Library (classic REST)
# ---------------------------------------------------------------------------

def classic_headers(cfg: dict) -> dict:
    def pick(keys: tuple) -> str:
        for key in keys:
            if cfg.get(key):
                return str(cfg[key])
        return ""
    token, secret, user = (pick(CLASSIC_TOKEN_KEYS), pick(CLASSIC_SECRET_KEYS),
                           pick(CLASSIC_USER_KEYS))
    if not (token and secret and user):
        raise RuntimeError(
            "classic document-library auth not configured in secret "
            "(needs ez_token/EZToken, ez_app_secret/EZAppSecret, "
            "account_username/AccountUsername)")
    return {"EZToken": token, "EZAppSecret": secret,
            "AccountUsername": user, "Accept": "application/json"}


def extract_document_records(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in DOCUMENT_RECORD_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [r for r in candidate if isinstance(r, dict)]
        if isinstance(candidate, dict):
            nested = extract_document_records(candidate)
            if nested:
                return nested
    return []


def document_fields(row: dict) -> dict:
    return {
        "name": first(row, "DocumentName", "documentName", "Name", "name",
                      "FileName", "fileName", "Description", "description"),
        "description": first(row, "Description", "description"),
        "policy_number": first(row, "PolicyNumber", "policyNumber",
                               "policy_number"),
        "policy_id": first(row, "PolicyId", "policyId", "policy_id"),
    }


def list_documents(cfg: dict, applicant_id: str, not_checked: list) -> dict:
    report: dict = {"read_method": None, "documents": [], "total_count": 0}
    try:
        headers = classic_headers(cfg)
    except RuntimeError as exc:
        not_checked.append(f"document library: {exc}")
        return report
    base = first(cfg, *CLASSIC_BASE_KEYS) or origin_of(cfg) + "/ezlynxapi/"
    base = base.rstrip("/") + "/"
    path = (f"api/documentlibrary/list/{quote(applicant_id, safe='')}/"
            f"1/{DOC_PAGE_SIZE}/0")
    report["read_method"] = "classic api/documentlibrary/list/{applicant}/1/200/0"
    parsed = urllib.parse.urlparse(base + path)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=CALL_TIMEOUT)
    try:
        conn.request("GET", parsed.path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
    finally:
        conn.close()
    report["http_status"] = status
    if status != 200:
        not_checked.append(
            f"document library: list returned HTTP {status}")
        return report
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        not_checked.append("document library: list returned non-JSON")
        return report
    rows = extract_document_records(payload)
    report["documents"] = [document_fields(r) for r in rows]
    report["total_count"] = len(report["documents"])
    return report


# ---------------------------------------------------------------------------
# jobs.db (read-only)
# ---------------------------------------------------------------------------

def connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_job(db_path: str, job_id: str, not_checked: list) -> dict:
    report: dict = {"job_id_requested": job_id, "found": False}
    if not Path(db_path).is_file():
        not_checked.append(f"job rows: database file not found at {db_path}")
        return report
    try:
        with connect_ro(db_path) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                report["error"] = "no jobs row with that id"
                return report
            report["found"] = True
            for key in ("id", "action_type", "status", "attempt_count",
                        "verification_count", "created_at", "updated_at",
                        "completed_at"):
                try:
                    report[key] = row[key]
                except (KeyError, IndexError):
                    pass
            last_error = str(row["last_error"] or "") if "last_error" in row.keys() else ""
            report["last_error_markers"] = find_markers(last_error)
            report["last_error_sha256"] = hashlib.sha256(
                last_error.encode()).hexdigest()
            report["last_error_excerpt"] = last_error[:1000]
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (json.JSONDecodeError, KeyError, IndexError):
                payload = {}
            report["payload_keys"] = sorted(payload.keys()) if isinstance(payload, dict) else []
            report["gmail_message_id"] = payload.get("gmail_message_id") if isinstance(payload, dict) else None
            report["checkpoints"] = []
            for cp in db.execute(
                    "SELECT kind, data_json, created_at FROM checkpoints "
                    "WHERE job_id=? ORDER BY id", (job_id,)):
                try:
                    data = json.loads(cp["data_json"] or "{}")
                except json.JSONDecodeError:
                    data = {}
                report["checkpoints"].append({
                    "kind": cp["kind"], "created_at": cp["created_at"],
                    "keys": sorted(data.keys()) if isinstance(data, dict) else [],
                    "markers": find_markers(cp["data_json"])})
            report["playwright_exec"] = []
            for call in db.execute(
                    "SELECT id, tool, status, created_at, updated_at, result_json "
                    "FROM playwright_exec WHERE job_id=? ORDER BY id", (job_id,)):
                report["playwright_exec"].append({
                    "id": call["id"], "tool": call["tool"],
                    "status": call["status"], "created_at": call["created_at"],
                    "updated_at": call["updated_at"],
                    "markers": find_markers(call["result_json"])})
            report["verification_evidence"] = [
                {"verified": item["verified"], "method": item["method"],
                 "source": item["source"], "authoritative": item["authoritative"],
                 "captured_at": item["captured_at"]}
                for item in db.execute(
                    "SELECT verified, method, source, authoritative, captured_at "
                    "FROM verification_evidence WHERE job_id=? ORDER BY id",
                    (job_id,))]
    except sqlite3.Error as exc:
        not_checked.append(f"job rows: sqlite error {type(exc).__name__}")
    return report


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only acceptance verifier")
    parser.add_argument("--applicant-id", required=True)
    parser.add_argument("--policy-number", required=True)
    parser.add_argument("--job-id", required=True,
                        help="Job ID passed as an input — never hardcoded.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--secret", required=True,
                        help="Secret Manager resource for the EZLynx API config")
    args = parser.parse_args()

    not_checked: list[str] = []
    report: dict = {
        "verifier": "verify_acceptance.py",
        "checked_at": utc_now(),
        "inputs": {
            "applicant_id": args.applicant_id,
            "policy_number": args.policy_number,
            "job_id": args.job_id,
        },
        "not_checked": not_checked,
    }

    try:
        cfg = load_secret(args.secret)
        token = get_token(cfg)
        origin = origin_of(cfg)
    except RuntimeError as exc:
        report["fatal"] = f"auth setup failed: {exc}"
        not_checked.append(f"all EZLynx reads: {exc}")
        print(json.dumps(report, indent=2, default=str))
        return 0

    report["named_policy"] = search_policy_by_number(
        origin, token, args.policy_number)
    report["applicant_policies"] = list_policies_on_applicant(
        origin, token, args.applicant_id, not_checked)
    report["documents"] = list_documents(cfg, args.applicant_id, not_checked)
    report["job"] = read_job(args.db_path, args.job_id, not_checked)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
