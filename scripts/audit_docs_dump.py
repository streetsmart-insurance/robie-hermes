#!/usr/bin/env python3
"""Dump audit-related docs/downloads/carrier contacts for Sep-5 template-only accounts."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from src.ezlynx.api_client import (
    EZLynxApiClient,
    document_library_total,
    extract_document_records,
)
from src.directory.ezlynx_directory_resolver import resolve_workflow, enrich_routing_from_directory

ACCOUNTS = [
    ("30438515", "Diamond Counseling LLC"),
    ("63877777", "Alvarado Enterprises LLC"),
    ("27278580", "Golden Rule Builders, LLC"),
    ("21588163", "Riverview Properties of Edison LLC"),
    ("138675525", "Norris Mechanical LLC"),
    ("187626597", "JCF Masonry LLC"),
    ("123506189", "Milzaz Kidz Academy LLC"),
    ("38245246", "Realty Improvement LLC"),
    ("190835268", "Panda Kitchen and Bath NJ Llc"),
]

AUDIT_NEEDLES = (
    "audit",
    "prmau",
    "premium audit",
    "payroll",
    "wc audit",
    "workers comp audit",
    "workers' comp audit",
    "audit request",
    "audit worksheet",
    "audit letter",
)

DOWNLOADS = Path("/opt/renewal-automation-system/data/downloads")
OUT = Path("/opt/renewal-automation-system/data/handoffs/audit_sep5_docs_for_email.json")


def is_auditish(name: str) -> bool:
    n = (name or "").lower()
    return any(x in n for x in AUDIT_NEEDLES)


def main() -> None:
    api = EZLynxApiClient()
    # load sep5 csv for policy/carrier
    csv_path = Path(
        "data/input_reports/EZLynx_Scheduled_1a07113a_Workers_Comp_Renewal_Audit_Queue_-_ROBIE_2026-09-05T0606.csv"
    )
    import csv

    by_id: Dict[str, Dict[str, str]] = {}
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                aid = str(row.get("Applicant ID") or "").strip()
                if aid:
                    by_id[aid] = row

    results = []
    for aid, name in ACCOUNTS:
        row = by_id.get(aid, {})
        carrier = (row.get("Master Company") or "").strip()
        policy = (row.get("Policy Number") or "").strip()
        entry: Dict[str, Any] = {
            "applicant_id": aid,
            "account": name,
            "policy_number": policy,
            "master_company": carrier,
            "document_library_auditish": [],
            "document_library_total": None,
            "document_library_error": None,
            "downloads_hits": [],
            "carrier_routing": None,
            "carrier_resolver": None,
        }

        # Document library
        try:
            docs_res = api.list_applicant_documents(aid, page_index=1, page_size=100)
            if docs_res.get("status") != "success":
                entry["document_library_error"] = docs_res
            else:
                records = extract_document_records(docs_res.get("data") or docs_res)
                entry["document_library_total"] = document_library_total(docs_res.get("data") or docs_res) or len(records)
                for rec in records:
                    # Description is filename in prod
                    desc = (
                        rec.get("Description")
                        or rec.get("DocumentName")
                        or rec.get("FileName")
                        or rec.get("name")
                        or ""
                    )
                    if is_auditish(str(desc)):
                        entry["document_library_auditish"].append(
                            {
                                "id": rec.get("Id") or rec.get("DocumentId") or rec.get("id"),
                                "description": desc,
                                "policy_id": rec.get("PolicyId") or rec.get("policy_id"),
                                "created": rec.get("CreatedDate") or rec.get("created") or rec.get("Created"),
                            }
                        )
                # if none matched, also include top 5 recent names for context
                if not entry["document_library_auditish"] and records:
                    recent = []
                    for rec in records[:8]:
                        desc = rec.get("Description") or rec.get("DocumentName") or ""
                        recent.append(
                            {
                                "id": rec.get("Id") or rec.get("DocumentId"),
                                "description": desc,
                                "created": rec.get("CreatedDate") or rec.get("created"),
                            }
                        )
                    entry["document_library_recent_sample"] = recent
        except Exception as e:
            entry["document_library_error"] = str(e)

        # Downloads folder
        if DOWNLOADS.exists():
            needles = [aid, name.split()[0], policy.replace(" ", "")]
            needles = [n for n in needles if n]
            for root, _dirs, files in os.walk(DOWNLOADS):
                for fn in files:
                    low = fn.lower()
                    path = str(Path(root) / fn)
                    if any(n.lower() in low or n.lower() in path.lower() for n in needles) and (
                        is_auditish(fn) or "audit" in path.lower() or aid in path
                    ):
                        entry["downloads_hits"].append(path)
                    elif aid in path or (policy and policy.replace(" ", "").lower() in low):
                        if fn.lower().endswith((".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx")):
                            entry["downloads_hits"].append(path)

        # Carrier routing
        if carrier:
            try:
                entry["carrier_resolver"] = resolve_workflow(carrier, "audit")
            except Exception as e:
                entry["carrier_resolver"] = {"error": str(e)}
            try:
                entry["carrier_routing"] = enrich_routing_from_directory(carrier)
            except Exception as e:
                entry["carrier_routing"] = {"error": str(e)}

        results.append(entry)
        print(
            "DONE",
            aid,
            name,
            "auditish",
            len(entry["document_library_auditish"]),
            "dl",
            len(entry["downloads_hits"]),
            "carrier",
            carrier,
        )

    # Also list any audit-named files in downloads globally that mention these accounts
    global_hits = []
    if DOWNLOADS.exists():
        for root, _dirs, files in os.walk(DOWNLOADS):
            for fn in files:
                if not is_auditish(fn):
                    continue
                path = str(Path(root) / fn)
                for aid, name in ACCOUNTS:
                    if aid in path or name.split()[0].lower() in fn.lower():
                        global_hits.append(path)
                        break

    out = {
        "source_csv": str(csv_path),
        "accounts": results,
        "global_audit_download_hits": global_hits[:50],
        "note": "Template-only Sep-5 accounts needing carrier/email audit docs for EZLynx Audit Request to Client attach",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print("WROTE", OUT)
    # compact summary for chat
    for r in results:
        print(
            "SUM",
            r["applicant_id"],
            "|",
            r["account"],
            "|",
            r["master_company"],
            "| auditish=",
            len(r["document_library_auditish"]),
            "| dl=",
            len(r["downloads_hits"]),
        )
        for d in r["document_library_auditish"][:5]:
            print("  DOC", d.get("id"), d.get("description"), d.get("created"))
        for p in r["downloads_hits"][:5]:
            print("  DL", p)
        cr = r.get("carrier_resolver") or {}
        if isinstance(cr, dict) and "error" not in cr:
            print("  RESOLVER", json.dumps(cr)[:300])


if __name__ == "__main__":
    main()
