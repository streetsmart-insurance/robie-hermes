#!/usr/bin/env python3
"""Run StreetSmart Insurance EZLynx Audit Verification Pipeline.

Usage:
    scripts/run_audit_verification.py --limit 3
    scripts/run_audit_verification.py --applicant 112237823
    scripts/run_audit_verification.py --applicant 30438515
    scripts/run_audit_verification.py --applicant 81168616
"""

import sys
import os
import csv
import json
import argparse
from pathlib import Path

# Ensure project root is in PYTHONPATH
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ezlynx.api_client import EZLynxApiClient
from src.ezlynx.audit_verification import AuditVerifier


def load_carrier_directory() -> dict:
    carriers = {}
    json_path = PROJECT_ROOT / "data" / "ezlynx_parsed_carrier_summary.json"
    if json_path.exists():
        try:
            with open(json_path) as f:
                cdata = json.load(f)
                for item in cdata:
                    name = item.get("carrier_name")
                    if name:
                        carriers[name] = item
        except Exception:
            pass
    return carriers


def find_latest_audit_report() -> Path:
    input_dir = PROJECT_ROOT / "data" / "input_reports"
    candidates = list(input_dir.glob("*Renewal_Audit_Queue*.csv"))
    if not candidates:
        candidates = list(input_dir.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError("No audit queue CSV reports found in data/input_reports")
    return max(candidates, key=os.path.getctime)


def main():
    parser = argparse.ArgumentParser(description="StreetSmart Insurance EZLynx Audit Verification Runner")
    parser.add_argument("--report", help="Path to audit queue CSV report")
    parser.add_argument("--applicant", help="Process single applicant ID")
    parser.add_argument("--policy", help="Target policy number (used with --applicant)")
    parser.add_argument("--limit", type=int, default=0, help="Max accounts to process")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    args = parser.parse_args()

    client = EZLynxApiClient()
    carrier_dir = load_carrier_directory()
    verifier = AuditVerifier(client, carrier_dir)

    rows_to_process = []
    if args.applicant:
        app_profile = client.get_applicant(args.applicant)
        app_info = app_profile.get("applicant", {}) if isinstance(app_profile, dict) else {}
        policies_payload = client.get_applicant_policies(args.applicant)
        policies = policies_payload.get("policies", []) if isinstance(policies_payload, dict) else []
        target_pol = None
        if args.policy:
            for p in policies:
                if p.get("PolicyNumber") == args.policy or args.policy in p.get("PolicyNumber", ""):
                    target_pol = p
                    break

        if not target_pol:
            # Sort by EffectiveDate descending and prioritize Active
            wc_pols = [p for p in policies if p.get("LOB", "").lower() in ["workers comp", "workers' compensation", "wc"]]
            active_wc = [p for p in wc_pols if str(p.get("Status", "")).lower() == "active"]
            candidates = active_wc if active_wc else (wc_pols if wc_pols else policies)
            if candidates:
                candidates.sort(key=lambda x: str(x.get("EffectiveDate") or ""), reverse=True)
                target_pol = candidates[0]

        rows_to_process.append({
            "Applicant ID": args.applicant,
            "Account Name": app_info.get("BusinessName") or app_info.get("ContactName") or "Applicant",
            "Policy Number": target_pol.get("PolicyNumber") if target_pol else (args.policy or "UNKNOWN"),
            "Master Company": target_pol.get("MasterCompanyName") if target_pol else "UNKNOWN",
            "Effective Date": (target_pol.get("EffectiveDate") or "2026-01-01")[:10] if target_pol else "2026-01-01",
            "Expiration Date": (target_pol.get("ExpirationDate") or "2027-01-01")[:10] if target_pol else "2027-01-01",
            "Line of Business": target_pol.get("LOB") if target_pol else "Workers comp",
            "CSR": target_pol.get("CSR") or "Automated Verification",
            "Assigned Producer": target_pol.get("Producer") or ""
        })
    else:
        report_file = Path(args.report) if args.report else find_latest_audit_report()
        with open(report_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows_to_process.append(row)

    if args.limit > 0:
        rows_to_process = rows_to_process[:args.limit]

    results = []
    for idx, row in enumerate(rows_to_process, 1):
        res = verifier.process_account(row)
        results.append(res)
        if not args.json:
            print("=" * 80)
            print(f"TEST RUN [{idx}/{len(rows_to_process)}]: {res['account_name']} (App ID: {res['applicant_id']})")
            print("=" * 80)
            print(f"  Policy #      : {res['policy_number']} ({res['lob']})")
            print(f"  Carrier       : {res['carrier']} -> {res['carrier_info'].get('matched_name')}")
            print(f"  Carrier Channel: {res['carrier_info'].get('channel')}")
            print(f"  State / Queue : {res['state']} | Discussion Title: '{res['discussion_title']}'")
            if res.get('existing_task'):
                et = res['existing_task']
                print(f"  Active Task   : '{et['task_desc']}' (Assigned to {et['assigned_to']} by {et['created_by']}, Due {et['due_date']})")
            print(f"  Contact Person: {res['evidence'].get('contact_person')}")
            print(f"  Contact Email : {res['evidence'].get('contact_email')}")
            print(f"  Contact Phone : {res['evidence'].get('contact_phone')}")
            
            # Step 1: Has carrier requested/issued audit?
            classified = res['evidence'].get('classified_documents', {})
            cur_stmts = classified.get('current_statements', [])
            hist_stmts = classified.get('historical_statements', [])
            req_docs = classified.get('requests', [])
            nc_docs = classified.get('non_compliance', [])
            
            print("\n  [Phase 1] Carrier Audit Document Status in EZLynx:")
            if cur_stmts:
                print(f"    FOUND Current Audit Statement: '{cur_stmts[0]['filename']}'")
            elif hist_stmts:
                print(f"    PENDING: Found prior term audit ('{hist_stmts[0]['filename']}'); current term missing.")
            elif req_docs:
                print(f"    FOUND Carrier Audit Request Questionnaire: '{req_docs[0]['filename']}'")
            elif nc_docs:
                print(f"    ALERT: Non-Compliance Notice on file ('{nc_docs[0]['filename']}')")
            else:
                print("    NOT IN EZLYNX: Carrier has not uploaded/requested audit in Document Library yet.")

            # Carrier Outreach
            if res.get('carrier_email_draft'):
                print("\n  [Phase 1b] Carrier Outreach (To Get Audit Docs):")
                print(f"    Email To  : {res['carrier_email_draft']['recipient']}")
                print(f"    Subject   : {res['carrier_email_draft']['subject']}")
            if res.get('carrier_voice_call_draft'):
                print(f"    Voice Call: Outbound Call Staged -> {res['carrier_voice_call_draft']['carrier']}")
                print(f"    AI Prompt : \"{res['carrier_voice_call_draft']['prompt'][:120]}...\"")

            # Client Delivery & Auto-dial
            if res.get('client_email_draft'):
                print("\n  [Phase 2] Client Delivery (To Complete Audit):")
                print(f"    Email To  : {res['client_email_draft']['recipient']} ({res['evidence'].get('contact_email')})")
                print(f"    Template  : {res['client_email_draft']['template_name']}")
                print(f"    Attach    : {res['client_email_draft']['attachment']}")
            if res.get('client_autodial_draft'):
                print("\n  [Phase 2b] Client Phone Auto-Dial Reminder:")
                print(f"    Dial To   : {res['client_autodial_draft']['phone']} ({res['client_autodial_draft']['recipient']})")
                print(f"    AI Prompt : \"{res['client_autodial_draft']['prompt'][:120]}...\"")

            print("\n  [Discussion Note Preview (ROBIE was here)]:")
            for note_line in res['draft_note'].splitlines():
                print(f"    | {note_line}")
            print("\n")

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
