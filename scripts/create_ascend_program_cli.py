#!/usr/bin/env python3
"""CLI utility to create an Ascend program from quote parameters or quote text and file to EZLynx."""

import argparse
import json
import sys
from pathlib import Path

# Add project root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robie_job_engine.ascend_workflow import AscendWorkflowManager


def main():
    parser = argparse.ArgumentParser(description="Create Ascend program and file into EZLynx")
    parser.add_argument("--quote-text", help="Raw email or quote text")
    parser.add_argument("--quote-file", help="Path to quote PDF")
    parser.add_argument("--instructions", default="", help="User instruction or clarification")
    parser.add_argument("--sender-email", default="", help="Email of requester")
    parser.add_argument("--sender-name", default="", help="Name of requester")
    parser.add_argument("--applicant-id", default=None, help="EZLynx applicant ID if known")
    args = parser.parse_args()

    manager = AscendWorkflowManager()
    
    content = ""
    if args.quote_file:
        content = Path(args.quote_file)
    elif args.quote_text:
        content = args.quote_text
    else:
        content = sys.stdin.read()

    result = manager.process_quote_request(
        raw_text_or_pdf=content,
        user_instruction=args.instructions,
        sender_email=args.sender_email,
        sender_name=args.sender_name,
        applicant_id=args.applicant_id,
    )

    out = {
        "status": result.status,
        "program_id": result.program_id,
        "program_url": result.program_url,
        "reply_email_subject": result.reply_email_subject,
        "reply_email_body": result.reply_email_body,
        "error": result.error,
        "quote": {
            "insured_name": result.quote.insured_name,
            "carrier_name": result.quote.carrier_name,
            "wholesaler_name": result.quote.wholesaler_name,
            "pure_premium_cents": result.quote.pure_premium_cents,
            "agency_fees_cents": result.quote.agency_fees_cents,
            "commission_rate": result.quote.commission_rate,
            "surplus_lines_tax_cents": result.quote.surplus_lines_tax_cents,
            "terrorism_included": result.quote.terrorism_included,
            "requires_hitl": result.quote.requires_hitl,
            "hitl_questions": result.quote.hitl_questions,
        }
    }
    print(json.dumps(out, indent=2))
    if result.status == "ERROR":
        sys.exit(1)


if __name__ == "__main__":
    main()
