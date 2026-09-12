#!/usr/bin/env python3
"""Acceptance verifier for the ROBIE email policy-setup path.

Read-only by construction. Given an applicant ID, a policy number, and a
job ID, it re-reads destination state from primary sources and reports
what is true — never what the worker claimed.

Reads performed (all from hermes-poc-01, all GETs, all with timeouts):
  1. PolicyApi policy search by number (OAuth):
       GET {origin}/PolicyApi/policy/v1/search?PolicyNumber=
     Reports whether the named policy exists and its field values
     (number, id, status, effective, expiration). Carrier IS reported:
     masterCompany and naicCode from the search row are the carrier
     identity (run 34697115179 returned masterCompany 13585 and naicCode
     "10048"). carrierID and writingCompany are also reported literally,
     but both read "0" on every policy seen -- they are not the carrier
     identity, and this note exists so nobody re-derives that conclusion.
  2. Applicant policy list attempt (OAuth):
       GET {origin}/PolicyApi/policy/v1/search?ApplicantId=
     If the endpoint answers, every returned row is listed
     (number, id, status, effective, expiration) with a rows-returned
     count — rows_returned is rows in the response, not a server total.
     Carrier is reported (see read #1).
     If it does not answer, the field is reported UNCHECKED with the
     reason — the verifier never invents a list. Only rows matching the
     requested applicant are printed; the rows-returned/honored/matching
     counts are the evidence the filter is not honored.
  3. Document Library (classic REST):
       GET {classic}/api/documentlibrary/list/{applicant}/1/200/0
     Reports every document row seen (name, description, policy number,
     policy id).
  3b. DocumentApi (OAuth, read-only):
       GET {origin}/documentapi/documents/v1/account/{ApplicantID}/document-search
     Proven path (PR #295). Reports every document with id, name,
     description, and which named policy (if any) it references.
  4. DiscussionApi (OAuth, read-only):
       GET {origin}/DiscussionApi/discussion/v1/applicant/{applicant}
     Reports every discussion/note with title and text excerpt, and
     which named policy (if any) it references.
  5. jobs.db (SQLite, read-only, query_only):
     The job's own rows for the job ID passed as --job-id (never
     hardcoded): the jobs row, checkpoints, playwright_exec calls, and
     verification_evidence.

Output is one JSON object on stdout with an explicit "not_checked" list
naming every field that could not be read and why.

Never prints secret values. Never writes anything.

Usage:
  sudo python3 verify_acceptance.py \
      --applicant-id 220250093 \
      --policy-number TEST-HO-20260912-D01,TEST-HO-08312026-01 \
      --job-id 1eeda98e-eedb-4758-8867-e9ac6116ac57 \
      --db-path /opt/streetsmart-hermes/robie-job-engine/data/jobs.db \
      --secret projects/751771086524/secrets/ezlynx-api-prod/versions/latest

  --policy-number accepts a comma-separated list; every named policy is
  read back independently. --job-id is optional; when omitted the job
  section is skipped and noted.
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
    "PLAYWRIGHT_TIMEOUT",
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


def find_server_total(payload: object) -> tuple:
    """(value, key): a server-side total from a response envelope, or
    (None, None). Prefers explicit total* keys and falls back to count*
    keys, reporting which envelope field the number came from — so a
    locally counted rows_returned is never mistaken for an authoritative
    server total. A bare-list response has no envelope: (None, None)."""
    if not isinstance(payload, dict):
        return None, None
    for keys in (("totalCount", "TotalCount", "total", "Total",
                  "totalRecords", "TotalRecords", "totalItems",
                  "TotalItems"),
                 ("count", "Count")):
        for key in keys:
            val = payload.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return val, key
    for nest in ("data", "Data", "result", "Result", "payload"):
        inner = payload.get(nest)
        if isinstance(inner, dict):
            val, key = find_server_total(inner)
            if val is not None:
                return val, key
    return None, None


def server_total_fields(payload: object) -> dict:
    """Report-shape fragment: the server's own total (if the envelope
    carries one) alongside the locally counted rows, labeled as such."""
    val, key = find_server_total(payload)
    fields = {"server_total_count": val, "server_total_key": key}
    if val is None:
        fields["server_total_note"] = (
            "response envelope carries no total; rows_returned is rows "
            "returned in this response, not a server total — if the "
            "endpoint paginates, the two differ silently")
    return fields


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
    # Carrier identity: the PolicyApi search row carries masterCompany and
    # naicCode, and those ARE the carrier identity (canary create read-back
    # in run 34697115179: masterCompany 13585, naicCode "10048"). carrierID
    # and writingCompany are ALSO reported literally below, but both read
    # "0" on every policy seen so far -- they are not the carrier identity,
    # and this note exists so nobody re-derives that wrong conclusion later.
    return {
        "policy_number": first(row, "PolicyNumber", "policyNumber", "policy_number"),
        "policy_id": first(row, "PolicyId", "policyId", "policy_id", "ID", "Id"),
        "status": first(row, "Status", "status", "PolicyStatus", "policyStatus"),
        "effective": first(row, "EffectiveDate", "effectiveDate", "effective",
                            "Effective", "PolicyEffectiveDate"),
        "expiration": first(row, "ExpirationDate", "expirationDate", "expiration",
                             "Expiration", "PolicyExpirationDate"),
        "applicant_id": first(row, "ApplicantId", "applicantId", "applicant_id",
                               "AccountId", "accountId"),
        "master_company": first(row, "MasterCompany", "masterCompany",
                                 "master_company"),
        "naic_code": first(row, "NaicCode", "naicCode", "naic_code",
                            "NAICCode", "NAICcode"),
        "carrier_id": first(row, "CarrierID", "carrierID", "carrier_id",
                             "CarrierId", "carrierId"),
        "writing_company": first(row, "WritingCompany", "writingCompany",
                                  "writing_company"),
        "carrier_note": (
            "masterCompany / naicCode are the carrier identity. carrierID "
            "and writingCompany both read \"0\" on every policy seen; they "
            "are not the carrier identity."
        ),
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
                    "policies": [], "rows_returned": 0,
                    "server_total_count": None, "server_total_key": None}
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
                report["rows_returned"] = len(policies)
                report.update(server_total_fields(payload))
                # The endpoint may ignore the applicant parameter and return a
                # global list. Never claim applicant scoping without checking
                # every returned row's own applicant_id.
                report["applicant_filter_honored"] = (
                    len(matching) == len(policies) and len(policies) > 0)
                report["policies_matching_applicant"] = len(matching)
                # Never print other applicants' policies into the log or the
                # artifact: the endpoint ignores the applicant parameter and
                # returns live policies belonging to other applicants (policy
                # numbers, ids, dates, carriers, applicant ids). Keep only
                # the rows that actually match the requested applicant; the
                # rows-returned/honored/matching counts above are the
                # evidence that the filter is not honored.
                report["policies"] = matching
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
    report: dict = {"read_method": None, "documents": [],
                    "rows_returned": 0, "server_total_count": None,
                    "server_total_key": None}
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
    report["rows_returned"] = len(report["documents"])
    report.update(server_total_fields(payload))
    return report


# ---------------------------------------------------------------------------
# DocumentApi (OAuth) + DiscussionApi (OAuth) — read-only GETs
# ---------------------------------------------------------------------------

def search_documents_oauth(origin: str, token: str, applicant_id: str,
                           not_checked: list) -> dict:
    """Proven path (PR #295): GET /documentapi/documents/v1/account/{id}/document-search."""
    report: dict = {
        "read_method": "documentapi/documents/v1/account/{applicant}/document-search",
        "http_status": None, "documents": [], "rows_returned": 0,
        "server_total_count": None, "server_total_key": None,
    }
    url = (origin + "/documentapi/documents/v1/account/"
           + quote(applicant_id, safe="") + "/document-search")
    status, payload = oauth_get(url, token)
    report["http_status"] = status
    if status != 200 or payload is None:
        not_checked.append(
            f"DocumentApi document-search: returned HTTP {status}")
        return report
    rows = extract_document_records(payload)
    if not rows and isinstance(payload, dict):
        for key in ("results", "Results"):
            cand = payload.get(key)
            if isinstance(cand, list):
                rows = [r for r in cand if isinstance(r, dict)]
                break
    for r in rows:
        report["documents"].append({
            "id": first(r, "id", "Id", "ID", "DocumentId", "documentId"),
            "name": first(r, "DocumentName", "documentName", "Name", "name",
                          "FileName", "fileName", "Title", "title"),
            "description": first(r, "Description", "description"),
            "policy_number": first(r, "PolicyNumber", "policyNumber",
                                   "policy_number"),
            "policy_id": first(r, "PolicyId", "policyId", "policy_id"),
            "mime": first(r, "MimeType", "mimeType", "ContentType",
                          "contentType"),
        })
    report["rows_returned"] = len(report["documents"])
    report.update(server_total_fields(payload))
    # The document-search rows mix real files with folder-like rows
    # (mime "unknown/unknown": "New Business/Application/Declarations",
    # "Renewal Offers/Declarations", "Certificate Requests",
    # "Proof of Insurance", "Policy Changes/Declarations", a row
    # literally named ")", etc.). A file count that includes those is
    # an overcount, so files and folder-like rows are counted
    # separately. Heuristic is mime-based and labeled as such.
    # Direction: file_count is a floor and folder_like_count a ceiling,
    # never the reverse — rows with mime "" or "unknown/unknown" are
    # counted as folder-like, so a real file with a blank mime lands in
    # folder_like_count.
    report["file_count"] = sum(
        1 for d in report["documents"]
        if d["mime"] not in ("", "unknown/unknown"))
    report["folder_like_count"] = (
        report["rows_returned"] - report["file_count"])
    report["folder_heuristic"] = 'mime == "unknown/unknown"'
    report["count_direction"] = (
        "file_count is a floor and folder_like_count a ceiling, never the "
        "reverse: rows with mime '' or 'unknown/unknown' count as "
        "folder-like, so a real file with a blank mime lands in "
        "folder_like_count")
    return report


def read_discussions_oauth(origin: str, token: str, applicant_id: str,
                           not_checked: list) -> dict:
    """GET /DiscussionApi/discussion/v1/applicant/{id}. Read-only."""
    report: dict = {
        "read_method": "DiscussionApi/discussion/v1/applicant/{applicant}",
        "http_status": None, "notes": [], "rows_returned": 0,
        "server_total_count": None, "server_total_key": None,
    }
    url = (origin + "/DiscussionApi/discussion/v1/applicant/"
           + quote(applicant_id, safe=""))
    status, payload = oauth_get(url, token)
    report["http_status"] = status
    if status != 200 or payload is None:
        not_checked.append(
            f"DiscussionApi applicant discussions: returned HTTP {status}")
        return report
    rows: list[dict] = []
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        for key in ("Discussions", "discussions", "Items", "items",
                    "Data", "data"):
            cand = payload.get(key)
            if isinstance(cand, list):
                rows = [r for r in cand if isinstance(r, dict)]
                break
        else:
            if payload.get("Title") or payload.get("Subject"):
                rows = [payload]
    for r in rows:
        text = first(r, "Text", "text", "Body", "body", "Message", "message",
                     "Note", "note", "Comments", "comments")
        report["notes"].append({
            "title": first(r, "Title", "title", "Subject", "subject"),
            "text_excerpt": text[:500],
            "created": first(r, "CreatedDate", "createdDate", "Created",
                             "created", "DateCreated", "dateCreated"),
            "author": first(r, "CreatedBy", "createdBy", "Author", "author",
                            "UserName", "userName"),
        })
    report["rows_returned"] = len(report["notes"])
    report.update(server_total_fields(payload))
    return report


def references_policy(haystack: str, policy_number: str) -> bool:
    return policy_number.strip().casefold() in (haystack or "").casefold()


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
    parser.add_argument("--policy-number", required=True,
                        help="Comma-separated policy number(s) to read back")
    parser.add_argument("--job-id", required=False, default=None,
                        help="Job ID passed as an input — never hardcoded. "
                             "Optional: when omitted the job section is skipped.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--secret", required=True,
                        help="Secret Manager resource for the EZLynx API config")
    args = parser.parse_args()

    policy_numbers = [p.strip() for p in args.policy_number.split(",")
                      if p.strip()]
    if not policy_numbers:
        print(json.dumps({"fatal": "no policy numbers given"}))
        return 0

    not_checked: list[str] = []
    report: dict = {
        "verifier": "verify_acceptance.py",
        "checked_at": utc_now(),
        "inputs": {
            "applicant_id": args.applicant_id,
            "policy_numbers": policy_numbers,
            "job_id": args.job_id,
        },
        "not_checked": not_checked,
    }

    # Load-bearing structural limits. These are true of every run because
    # they are properties of the PolicyApi, not of this check.
    not_checked.extend([
        "PolicyApi has no applicant-scoped policy-list endpoint: "
        "search?ApplicantId= returns unfiltered account rows.",
        f"the verifier cannot enumerate applicant {args.applicant_id}: "
        "only per-policy search?PolicyNumber= is supported.",
        "the verifier cannot detect a duplicate policy created under a "
        "different policy number: it only reads the exact numbers named.",
        "the verifier cannot prove the account was clean before a test: "
        "it reads current destination state, not history.",
    ])

    try:
        cfg = load_secret(args.secret)
        token = get_token(cfg)
        origin = origin_of(cfg)
    except RuntimeError as exc:
        report["fatal"] = f"auth setup failed: {exc}"
        not_checked.append(f"all EZLynx reads: {exc}")
        print(json.dumps(report, indent=2, default=str))
        return 0

    report["named_policies"] = [
        search_policy_by_number(origin, token, pn)
        for pn in policy_numbers
    ]
    report["applicant_policies"] = list_policies_on_applicant(
        origin, token, args.applicant_id, not_checked)
    report["documents"] = list_documents(cfg, args.applicant_id, not_checked)
    report["document_api"] = search_documents_oauth(
        origin, token, args.applicant_id, not_checked)
    report["discussions"] = read_discussions_oauth(
        origin, token, args.applicant_id, not_checked)

    # Per-policy attachment/note read-back, matched literally by policy
    # number appearing in the document/note record. Attachment matching
    # uses file rows only: folder-like rows (mime unknown/unknown) cannot
    # be PDF attachments.
    file_rows = [d for d in report["document_api"]["documents"]
                 if d["mime"] not in ("", "unknown/unknown")]
    for entry, pn in zip(report["named_policies"], policy_numbers):
        docs = [d for d in file_rows
                if references_policy(
                    " ".join([d["name"], d["description"],
                              d["policy_number"], d["policy_id"]]), pn)]
        entry["pdf_attached"] = any(
            "pdf" in d["name"].casefold() or "pdf" in d["mime"].casefold()
            for d in docs)
        entry["attached_documents"] = [
            {"name": d["name"], "description": d["description"],
             "mime": d["mime"]} for d in docs]
        notes = [n for n in report["discussions"]["notes"]
                 if references_policy(n["title"] + " " + n["text_excerpt"], pn)]
        entry["note_posted"] = bool(notes)
        entry["matching_notes"] = [
            {"title": n["title"], "text_excerpt": n["text_excerpt"][:200]}
            for n in notes]

    if args.job_id:
        report["job"] = read_job(args.db_path, args.job_id, not_checked)
    else:
        not_checked.append("job rows: no --job-id given; job section skipped")

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
