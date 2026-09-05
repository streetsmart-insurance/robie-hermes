#!/usr/bin/env python3
"""Universal CLI for EZLynx Operations across all Agent Skills and Tools.

Usage:
    scripts/ezlynx_cli.py search "Acme" [--json]
    scripts/ezlynx_cli.py applicant 21587333 [--json]
    scripts/ezlynx_cli.py policies 21587333 [--json]
    scripts/ezlynx_cli.py documents 21587333 [--json]
    scripts/ezlynx_cli.py note 21587333 "Manual Renewal" "Underwriter outreach initiated."
    scripts/ezlynx_cli.py quote <quote_id> [--json]
    scripts/ezlynx_cli.py sessions [--json]
"""

import sys
import os
import json
import argparse
from pathlib import Path

# Ensure project root is in PYTHONPATH
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ezlynx.api_client import EZLynxApiClient


def main():
    parser = argparse.ArgumentParser(description="Universal EZLynx CLI for StreetSmart Agents & Tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Search
    p_search = subparsers.add_parser("search", help="Search applicants by name, DBA, ID, or policy number")
    p_search.add_argument("query", help="Search query string")
    p_search.add_argument("--json", action="store_true", help="Output raw JSON")

    # 2. Applicant
    p_app = subparsers.add_parser("applicant", help="Get applicant profile details")
    p_app.add_argument("applicant_id", help="EZLynx Applicant ID")
    p_app.add_argument("--json", action="store_true", help="Output raw JSON")

    # 3. Policies
    p_pol = subparsers.add_parser("policies", help="Get active and historical policies for applicant")
    p_pol.add_argument("applicant_id", help="EZLynx Applicant ID")
    p_pol.add_argument("--start-date", default="2020-01-01", help="Start date (YYYY-MM-DD)")
    p_pol.add_argument("--end-date", default="2027-12-31", help="End date (YYYY-MM-DD)")
    p_pol.add_argument("--json", action="store_true", help="Output raw JSON")

    # 4. Documents
    p_doc = subparsers.add_parser("documents", help="List documents in applicant Document Library")
    p_doc.add_argument("applicant_id", help="EZLynx Applicant ID")
    p_doc.add_argument("--page", type=int, default=1, help="Page index")
    p_doc.add_argument("--size", type=int, default=20, help="Page size")
    p_doc.add_argument("--json", action="store_true", help="Output raw JSON")

    # 5. Note
    p_note = subparsers.add_parser("note", help="Post note to applicant discussion card (auto-matching active cards)")
    p_note.add_argument("applicant_id", help="EZLynx Applicant ID")
    p_note.add_argument("title_or_text", help="Discussion title OR note text if --policy-number/--title is used")
    p_note.add_argument("note_text", nargs="?", default=None, help="Note text (when title is passed as positional arg)")
    p_note.add_argument("--title", default=None, help="Explicit discussion title")
    p_note.add_argument("--policy-number", default=None, help="Associated policy number for auto-matching card")
    p_note.add_argument("--lob", default=None, help="Line of business for fuzzy matching")
    p_note.add_argument("--carrier", default=None, help="Carrier name for fuzzy matching")
    p_note.add_argument("--json", action="store_true", help="Output raw JSON")

    # 6. Discussions
    p_disc = subparsers.add_parser("discussions", help="List active discussion cards for an applicant")
    p_disc.add_argument("applicant_id", help="EZLynx Applicant ID")
    p_disc.add_argument("--size", type=int, default=15, help="Number of discussions to retrieve")
    p_disc.add_argument("--json", action="store_true", help="Output raw JSON")

    # 7. Sessions
    p_sess = subparsers.add_parser("sessions", help="View health of API tokens and browser sessions")
    p_sess.add_argument("--json", action="store_true", help="Output raw JSON")

    # 8. Quote
    p_quote = subparsers.add_parser("quote", help="Get completed comparative rating quote session")
    p_quote.add_argument("quote_id", help="EZLynx Quote ID")
    p_quote.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()
    client = EZLynxApiClient()

    if args.command == "search":
        results = client.search_applicants(args.query)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(f"Found {len(results)} match(es) for '{args.query}':")
            for r in results:
                name = r.get("name") or "Unknown"
                app_id = r.get("applicant_id")
                agent = r.get("assigned_to") or "Unassigned"
                email = r.get("email") or "No Email"
                phone = r.get("phone") or "No Phone"
                print(f"  • ID: {app_id} | Name: {name} | Agent: {agent} | Email: {email} | Phone: {phone}")

    elif args.command == "applicant":
        res = client.get_applicant(args.applicant_id)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("status") == "success":
                app = res.get("applicant", {})
                print(f"Applicant #{args.applicant_id}:")
                print(f"  Business Name : {app.get('BusinessName') or 'N/A'}")
                print(f"  Contact Name  : {app.get('FirstName', '')} {app.get('LastName', '')}".strip() or "N/A")
                print(f"  Type          : {app.get('ApplicantType')}")
                print(f"  Assigned Agent: {app.get('AssignedTo')}")
                print(f"  Email         : {app.get('BusinessEmail') or app.get('Email') or 'N/A'}")
                print(f"  Phone         : {app.get('BusinessPhone') or app.get('CellPhone') or 'N/A'}")
                addr = app.get("CurrentAddress") or {}
                print(f"  Address       : {addr.get('AddressLine1', '')}, {addr.get('City', '')} {addr.get('State', '')} {addr.get('Zip', '')}".strip(" ,"))
            else:
                print(f"Error fetching applicant: {res.get('error')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "policies":
        res = client.get_applicant_policies(args.applicant_id, args.start_date, args.end_date)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("status") == "success":
                pols = res.get("policies", [])
                print(f"Found {len(pols)} policy record(s) for Applicant #{args.applicant_id}:")
                for p in pols:
                    pol_num = p.get("PolicyNumber")
                    lob = p.get("LOB")
                    carrier = p.get("Company") or p.get("Carrier") or "N/A"
                    eff = str(p.get("EffectiveDate", ""))[:10]
                    exp = str(p.get("ExpirationDate", ""))[:10]
                    print(f"  • #{pol_num} | LOB: {lob} | Carrier: {carrier} | Eff: {eff} | Exp: {exp}")
            else:
                print(f"Error fetching policies: {res.get('error')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "documents":
        res = client.list_applicant_documents(args.applicant_id, args.page, args.size)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("status") == "success":
                docs = res.get("data", [])
                print(f"Document Library for Applicant #{args.applicant_id}:")
                if isinstance(docs, list):
                    for d in docs:
                        print(f"  • ID: {d.get('DocumentID')} | Name: {d.get('DocumentName')} | Date: {d.get('CreatedDate')}")
                elif isinstance(docs, dict):
                    records = docs.get("Records", []) or docs.get("DocumentList", [])
                    print(f"  Total records: {docs.get('TotalRecords', len(records))}")
                    for d in records:
                        print(f"  • ID: {d.get('DocumentID') or d.get('Id')} | Name: {d.get('DocumentName') or d.get('Name')}")
            else:
                print(f"Error listing documents: {res.get('error')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "note":
        if args.note_text is not None:
            disc_title = args.title or args.title_or_text
            note_content = args.note_text
        else:
            disc_title = args.title
            note_content = args.title_or_text

        res = client.add_note_to_discussion(
            applicant_id=args.applicant_id,
            discussion_title=disc_title,
            note_text=note_content,
            policy_number=args.policy_number,
            line_of_business=args.lob,
            carrier_name=args.carrier
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Note result: Status={res.get('status')} | Method={res.get('method')} | Applicant={args.applicant_id}")
            if res.get("note_id"):
                print(f"  Note ID: {res.get('note_id')}")
            if res.get("discussion_title"):
                print(f"  Discussion Title: {res.get('discussion_title')}")

    elif args.command == "discussions":
        discussions = client.get_applicant_discussions(args.applicant_id, page_size=args.size)
        if args.json:
            print(json.dumps(discussions, indent=2))
        else:
            print(f"Active Discussions for Applicant #{args.applicant_id} ({len(discussions)} found):")
            for d in discussions:
                d_id = d.get("discussionId")
                title = d.get("title")
                notes = d.get("noteCount", 0)
                tasks = d.get("taskCount", 0)
                mod_by = d.get("lastModifiedByName") or "N/A"
                mod_date = str(d.get("lastModifiedDate", ""))[:16].replace("T", " ") if d.get("lastModifiedDate") else "N/A"
                print(f"  • [{d_id}] {title} (Notes: {notes}, Tasks: {tasks}, Modified: {mod_date} by {mod_by})")

    elif args.command == "sessions":
        overview = client.get_session_overview()
        if args.json:
            print(json.dumps(overview, indent=2))
        else:
            print("EZLynx Sessions Overview:")
            c = overview["classic_api"]
            print(f"  Classic API    : {'CONNECTED' if c['authenticated'] else 'OFFLINE'} ({c['username']})")
            o = overview["oauth_gateway"]
            print(f"  OAuth2 Gateway : {'CONNECTED' if o['authenticated'] else 'OFFLINE'} (Scopes: {', '.join(o['scopes'])})")
            b = overview["browser_session"]
            print(f"  Chrome CDP     : {'ACTIVE' if b['cdp_connected'] else 'DISCONNECTED'} ({b['cdp_endpoint']})")
            print(f"  Browser Storage: {'PRESENT' if b['exists'] else 'EMPTY'} ({b['cookie_count']} cookies)")

    elif args.command == "quote":
        res = client.get_completed_quote(args.quote_id)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res.get("status") == "success":
                qd = res.get("quote_data", {})
                print(f"Quote Session #{args.quote_id} (Applicant: {qd.get('ApplicantId')} | State: {qd.get('RatingState')}):")
                for r in qd.get("QuoteResults", []):
                    prem = f"${r.get('Premium', 0):,.2f}" if r.get('Premium') is not None else "N/A"
                    print(f"  • Carrier: {r.get('CarrierName')} | Premium: {prem} | Status: {r.get('Status')} | LOB: {r.get('LOB')}")
            else:
                print(f"Error fetching quote session: {res.get('error')}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
