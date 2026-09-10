#!/usr/bin/env python3
"""
Robie Carrier Policy Change Voice AI Dispatcher.

Autonomous outbound voice caller powered by Bland AI to follow up on
outstanding carrier endorsements and policy change declarations.
"""

import argparse
import json
import logging
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
except Exception:
    pass
from robie_guard import (  # noqa: E402
    WriteNotAuthorized, add_write_gate_args, assert_write_allowed, resolve_write_gate,
)

logger = logging.getLogger("policy_change_caller")

# Default voice settings
DEFAULT_CALLER_ID = "+17322986745"
DEFAULT_VOICE_KEY = os.getenv("VOICE_AI_API_KEY")

# Check fallback env file if not set in environment
if not DEFAULT_VOICE_KEY:
    env_paths = [
        Path("/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system/.env"),
        Path("/Users/carloferrara/Documents/antigravity/busy-borg/.env"),
        Path(__file__).resolve().parent.parent / ".env"
    ]
    for p in env_paths:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("VOICE_AI_API_KEY="):
                    DEFAULT_VOICE_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("VOICE_CALLER_ID="):
                    DEFAULT_CALLER_ID = line.split("=", 1)[1].strip().strip('"').strip("'")

# Built-in carrier matrix fallback
KNOWN_CARRIERS = {
    "merchants": {
        "name": "Merchants Insurance Group",
        "phone": "+18004621077",
        "agency_code": "84409",
        "ivr": "Press 2 for Commercial Lines, then 1 for Policy Servicing / Endorsements. UW Maritza Santiago: msantiago@merchantsgroup.com",
    },
    "progressive": {
        "name": "Progressive Commercial",
        "phone": "+18777762436",
        "agency_code": "33617",
        "ivr": "Enter Agent Code 33617. Press 2 for Existing Commercial Auto Policy Service.",
    },
    "national general": {
        "name": "National General",
        "phone": "+18883251190",
        "agency_code": "9010158",
        "ivr": "Enter Agency Code 9010158. Press 2 for Commercial Auto Endorsements & Policy Servicing.",
    },
    "selective": {
        "name": "Selective Insurance",
        "phone": "+18777443125",
        "agency_code": "008911",
        "ivr": "Press 2 for Agency Servicing, enter Agent Code 008911. Press 3 for Commercial Lines Endorsements.",
    },
    "utica first": {
        "name": "Utica First Insurance",
        "phone": "+18004564556",
        "agency_code": "On File",
        "ivr": "Press 2 for Commercial Underwriting / Endorsement Processing. UW Heather Burgdoff: hburgdoff@uticafirst.com",
    },
    "franklin mutual": {
        "name": "Franklin Mutual Insurance",
        "phone": "+19739483120",
        "agency_code": "165100",
        "ivr": "Press 1 for Commercial Lines Underwriting, then 2 for Policy Changes.",
    },
    "fmi": {
        "name": "Franklin Mutual Insurance",
        "phone": "+19739483120",
        "agency_code": "165100",
        "ivr": "Press 1 for Commercial Lines Underwriting, then 2 for Policy Changes.",
    },
    "geico": {
        "name": "Geico Commercial",
        "phone": "+18006242513",
        "agency_code": "G01589",
        "ivr": "Press 2 for Agency Service, then 3 for Commercial Auto Policy Changes.",
    },
    "amtrust": {
        "name": "AmTrust North America",
        "phone": "+18775287878",
        "agency_code": "58388",
        "ivr": "Enter Agent Code 58388. Press 2 for Workers Comp Underwriting & Policy Changes.",
    },
    "coterie": {
        "name": "Coterie Insurance",
        "phone": "+18555661011",
        "agency_code": "Agency Direct",
        "ivr": "Press 1 for Agent Support & Endorsements.",
    },
    "rt specialty": {
        "name": "RT Specialty / Interstate MGA",
        "phone": "+18772759578",
        "agency_code": "On File",
        "ivr": "Commercial Lines Endorsements. Ask for Caroline Shaddow. DO NOT route to QuickHome.",
    },
    "jimcor": {
        "name": "JIMCOR Agencies",
        "phone": "+12015738200",
        "agency_code": "Agt8572",
        "ivr": "Ask for Arlene Rivera (ARivera@jimcor.com) or Markel Excess Endorsement team.",
    },
    "johnson & johnson": {
        "name": "Johnson & Johnson Insurance (MGA)",
        "phone": "+18004877565",
        "agency_code": "895543",
        "ivr": "Wholesale broker / MGA for Lloyd's of London & specialty policies. Do NOT route to JJPF financing.",
    },
    "j&j": {
        "name": "Johnson & Johnson Insurance (MGA)",
        "phone": "+18004877565",
        "agency_code": "895543",
        "ivr": "Wholesale broker / MGA for Lloyd's of London & specialty policies. Do NOT route to JJPF financing.",
    },
    "berkshire hathaway": {
        "name": "Berkshire Hathaway Homestate Companies",
        "phone": "+18004882930",
        "agency_code": "On File",
        "ivr": "Commercial Auto Underwriting & Policy Servicing. Direct email: bhservices@bhhomestate.com / auto@bhhomestate.com",
    },
    "bhhc": {
        "name": "Berkshire Hathaway Homestate Companies",
        "phone": "+18004882930",
        "agency_code": "On File",
        "ivr": "Commercial Auto Underwriting & Policy Servicing. Direct email: bhservices@bhhomestate.com / auto@bhhomestate.com",
    },
    "guard": {
        "name": "Berkshire Hathaway GUARD",
        "phone": "+18006732465",
        "agency_code": "On File",
        "ivr": "Customer Service & Policy Servicing Department.",
    },
}


def is_within_carrier_calling_hours(tz_name: str = "America/New_York") -> tuple[bool, str]:
    """
    Enforces strict carrier business hours:
    - Monday through Friday only (weekdays 0-4)
    - Between 9:00 AM and 6:00 PM Eastern Time
    - Excludes US federal / carrier holidays (e.g. Labor Day, Memorial Day, New Year, July 4, Thanksgiving, Christmas)
    """
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        import pytz
        now = datetime.now(pytz.timezone(tz_name))

    weekday = now.weekday()
    if weekday >= 5:
        return False, f"Outside business days (Weekend: {now.strftime('%A')}). Calling hours are Mon-Fri 9:00 AM - 6:00 PM ET."

    hour = now.hour
    if hour < 9:
        return False, f"Too early: {now.strftime('%I:%M %p %Z')}. Outbound calling begins at 9:00 AM ET."
    if hour >= 18:
        return False, f"Too late: {now.strftime('%I:%M %p %Z')}. Outbound calling closes at 6:00 PM ET."

    month, day = now.month, now.day
    # Fixed federal holidays
    if (month == 1 and day == 1) or (month == 7 and day == 4) or (month == 12 and day == 25):
        return False, f"Federal / Carrier holiday ({now.strftime('%B %d')}). Offices are closed."
    # Labor Day (first Monday of September)
    if month == 9 and weekday == 0 and 1 <= day <= 7:
        return False, f"Labor Day holiday. Carrier and broker offices are closed."
    # Memorial Day (last Monday of May)
    if month == 5 and weekday == 0 and day >= 25:
        return False, f"Memorial Day holiday. Carrier and broker offices are closed."
    # Thanksgiving (fourth Thursday of November)
    if month == 11 and weekday == 3 and 22 <= day <= 28:
        return False, f"Thanksgiving Day holiday. Carrier and broker offices are closed."

    return True, f"Within business hours ({now.strftime('%I:%M %p %Z')})."


def resolve_carrier_details(carrier_name: Optional[str], phone_override: Optional[str]) -> Dict[str, Any]:
    """Resolves carrier contact information from known directory or phone override."""
    res = {
        "name": carrier_name or "Carrier Servicing Desk",
        "phone": phone_override,
        "agency_code": "On File",
        "ivr": None,
    }
    if not carrier_name:
        return res

    clean = carrier_name.lower().strip()
    for key, data in KNOWN_CARRIERS.items():
        if key in clean or clean in key:
            res["name"] = data["name"]
            if not res["phone"]:
                res["phone"] = data["phone"]
            res["agency_code"] = data.get("agency_code", "On File")
            res["ivr"] = data.get("ivr")
            break

    # Also check ezlynx_all_organizations.json if phone still missing
    if not res["phone"]:
        ez_orgs_path = Path("/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system/data/ezlynx_all_organizations.json")
        if ez_orgs_path.exists():
            try:
                orgs = json.loads(ez_orgs_path.read_text(encoding="utf-8"))
                for o in orgs:
                    org_name = o.get("name", "").lower()
                    # Skip financial/financing companies
                    if "financing" in org_name or "financial" in org_name:
                        continue
                    if clean in org_name:
                        p = o.get("phone")
                        if p:
                            clean_p = re.sub(r"[^\d+]", "", str(p))
                            if not clean_p.startswith("+") and len(clean_p) == 10:
                                clean_p = f"+1{clean_p}"
                            res["phone"] = clean_p
                            break
            except Exception as e:
                logger.warning(f"Failed to search ezlynx organizations: {e}")

    return res


def build_policy_change_prompt(
    policy_number: str,
    carrier_name: str,
    insured_name: str,
    agency_code: str,
    change_summary: str,
    submission_date: Optional[str] = None,
    ivr_guidance: Optional[str] = None,
    custom_instructions: Optional[str] = None,
) -> str:
    """Builds conversational instructions tailored specifically for policy change retrieval."""
    sub_clause = f" submitted on {submission_date}" if submission_date else ""
    ivr_clause = f"\nPhone menu / IVR navigation guidance: {ivr_guidance}" if ivr_guidance else ""
    custom_clause = f"\nAdditional CSR Notes: {custom_instructions}" if custom_instructions else ""

    prompt = f"""You are Robie, an autonomous operations and policy servicing specialist calling from StreetSmart Insurance.

CALL DETAILS:
- Target Carrier: {carrier_name}
- Policy Number: {policy_number}
- Insured Legal Name: {insured_name}
- Agency Producer Code: {agency_code}
- Change Requested: {change_summary}{sub_clause}{ivr_clause}{custom_clause}

CALL OBJECTIVES:
1. When navigating automated phone trees (IVR), select options for Policy Servicing, Commercial/Personal Endorsements, or Underwriting.
2. If placed on hold with music or chimes, remain on the line patiently and DO NOT speak until a live human representative greets you.
3. Once connected to a live representative:
   - Introduce yourself warmly: "Hello! My name is Robie calling from StreetSmart Insurance. Our agency producer code is {agency_code}."
   - State the exact inquiry: "I am following up on a policy change endorsement{sub_clause} for {insured_name}, policy number {policy_number}."
   - Describe the change: "The request was to {change_summary}."
4. Check Issuance Status:
   - Inquire: "Has the endorsement schedule or revised declarations page been processed and issued?"
5. If the change has been processed:
   - Inquire about the revised premium or billing adjustment: "Was there any additional or return premium generated?"
   - Request document delivery: "Could you please email the endorsement declaration PDF to robie@streetsmart.insurance, or confirm if it is ready on the agent portal?"
6. If the change is still pending underwriter review:
   - Inquire what is needed: "Is there any outstanding item or signed form required from our agency or the insured to release this change?"
   - Ask for the assigned underwriter's name, direct email, or expected turnaround time.
7. Conclude the call:
   - Ask for the representative's first name and case/reference number if available.
   - Thank them courteously: "Thank you so much for your assistance, [Rep Name]. Have a great day!"
"""
    return prompt


def dispatch_policy_change_call(
    policy_number: str,
    carrier_name: str,
    insured_name: str,
    change_summary: str,
    phone_override: Optional[str] = None,
    submission_date: Optional[str] = None,
    custom_instructions: Optional[str] = None,
    dry_run: bool = True,
    api_key: Optional[str] = None,
    caller_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Hydrates calling context and dispatches call via Bland AI or simulation."""
    carrier_info = resolve_carrier_details(carrier_name, phone_override)
    target_phone = carrier_info.get("phone")
    agency_code = carrier_info.get("agency_code", "On File")
    ivr_guidance = carrier_info.get("ivr")

    if not target_phone:
        logger.error(f"Cannot dispatch call: No phone number resolved for {carrier_name} / {policy_number}.")
        return {
            "success": False,
            "error": "NO_CARRIER_PHONE",
            "carrier": carrier_name,
            "policy": policy_number,
        }

    # Format phone with +1 if 10 digits
    clean_phone = re.sub(r"[^\d+]", "", target_phone)
    if not clean_phone.startswith("+"):
        if len(clean_phone) == 10:
            clean_phone = f"+1{clean_phone}"
        elif len(clean_phone) == 11 and clean_phone.startswith("1"):
            clean_phone = f"+{clean_phone}"

    prompt = build_policy_change_prompt(
        policy_number=policy_number,
        carrier_name=carrier_info["name"],
        insured_name=insured_name,
        agency_code=agency_code,
        change_summary=change_summary,
        submission_date=submission_date,
        ivr_guidance=ivr_guidance,
        custom_instructions=custom_instructions,
    )

    resolved_key = api_key or DEFAULT_VOICE_KEY
    resolved_caller_id = caller_id or DEFAULT_CALLER_ID

    # Dry-Run / Simulation
    if dry_run or not resolved_key:
        logger.info(f"[SIMULATION] Policy change call staged for {policy_number} -> {carrier_info['name']} ({clean_phone})")
        return {
            "success": True,
            "mode": "SIMULATION",
            "call_id": f"sim_call_change_{policy_number.replace(' ', '_')}",
            "carrier": carrier_info["name"],
            "policy": policy_number,
            "insured": insured_name,
            "phone": clean_phone,
            "agency_code": agency_code,
            "prompt": prompt,
            "status": "DISPATCHED_SIMULATED",
        }

    # HARDENED INVARIANT 7: Business Hours Gate (Mon-Fri 9:00 AM - 6:00 PM Eastern Time)
    within_hours, hours_msg = is_within_carrier_calling_hours()
    if not within_hours:
        logger.warning(f"[BUSINESS_HOURS_GATE] Call blocked for {policy_number} to {carrier_info['name']}: {hours_msg}")
        return {
            "success": False,
            "error": "CALL_BLOCKED_AFTER_HOURS",
            "carrier": carrier_info["name"],
            "policy": policy_number,
            "phone": clean_phone,
            "details": hours_msg,
            "status": "QUEUED_FOR_BUSINESS_HOURS",
        }

    # Live Bland AI Dispatch
    url = "https://api.bland.ai/v1/calls"
    headers = {
        "Authorization": resolved_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    payload = {
        "phone_number": clean_phone,
        "task": prompt,
        "voice": "nat",
        "model": "enhanced",
        "record": True,
        "answered_by_enabled": True,
        "wait_for_greeting": True,
        "ivr_navigation": True,
        "first_sentence": f"Hello! My name is Robie calling from StreetSmart Insurance regarding policy number {policy_number}.",
        "voicemail_action": "leave_message",
        "voicemail_message": (
            f"Hello, this is Robie from StreetSmart Insurance calling regarding Policy #{policy_number} "
            f"for {insured_name}. We are following up on the policy change request submitted. "
            "Please email any documents or updates to robie@streetsmart.insurance. Thank you!"
        ),
        "metadata": {
            "policy_number": policy_number,
            "insured_name": insured_name,
            "carrier_name": carrier_info["name"],
            "change_summary": change_summary,
            "type": "policy_change_endorsement_followup",
        },
    }
    if resolved_caller_id:
        payload["from"] = resolved_caller_id

    try:
        import requests
        resp = requests.post(url, json=payload, headers=headers, timeout=25)
        # If rejected due to 'from' ownership/concurrency, retry once without 'from'
        if resp.status_code in (400, 422) and "from" in payload:
            logger.warning(f"Bland AI rejected 'from' caller ID ({resolved_caller_id}). Retrying with default outbound pool...")
            payload.pop("from", None)
            resp = requests.post(url, json=payload, headers=headers, timeout=25)

        resp_data = resp.json() if resp.text else {}
        if resp.status_code in (200, 201):
            call_id = resp_data.get("call_id")
            # Auto-post dispatch note to EZLynx
            try:
                from src.ezlynx.api_client import EZLynxApiClient
                client = EZLynxApiClient()
                search_hit = client.search_applicant(policy_number)
                applicant_id = search_hit.get("applicant_id") if search_hit else None
                if applicant_id:
                    note_body = f"""=== [AUTONOMOUS POLICY CHANGE CALL DISPATCHED] ===
Policy: #{policy_number} ({carrier_info['name']})
Insured: {insured_name}
Target Carrier Desk: {clean_phone}
Purpose: {change_summary}
Telephony Engine: Bland AI (Call ID: {call_id})

ROBIE was here"""
                    assert_write_allowed(
                        "add_note_to_discussion",
                        applicant_id=str(applicant_id),
                        policy_number=policy_number,
                        detail="carrier call dispatch note",
                    )
                    client.add_note_to_discussion(
                        applicant_id=str(applicant_id),
                        discussion_title=f"Policy Change Call | {policy_number} {carrier_info['name']}",
                        note_text=note_body,
                        policy_number=policy_number,
                        carrier_name=carrier_info["name"],
                    )
            except WriteNotAuthorized as gate_err:
                logger.warning("[WRITE_GATE] EZLynx dispatch note NOT posted: %s", gate_err)
            except Exception as ez_err:
                logger.warning(f"Failed to post caller dispatch to EZLynx: {ez_err}")

            return {
                "success": True,
                "mode": "LIVE_BLAND_AI",
                "call_id": call_id,
                "carrier": carrier_info["name"],
                "policy": policy_number,
                "phone": clean_phone,
                "status": resp_data.get("status", "queued"),
                "raw_response": resp_data,
            }
        else:
            logger.error(f"Bland AI API error {resp.status_code}: {resp.text}")
            return {
                "success": False,
                "error": "API_ERROR",
                "status_code": resp.status_code,
                "carrier": carrier_info["name"],
                "policy": policy_number,
                "phone": clean_phone,
                "details": resp_data,
            }
    except Exception as e:
        logger.error(f"Failed to dispatch Bland AI call: {e}")
        return {
            "success": False,
            "error": "HTTP_ERROR",
            "message": str(e),
            "carrier": carrier_info["name"],
            "policy": policy_number,
            "phone": clean_phone,
        }


def format_ezlynx_note(res: Dict[str, Any], change_summary: str) -> str:
    """Generates the formatted EZLynx discussion card note concluding with ROBIE was here."""
    phone = res.get("phone", "N/A")
    carrier = res.get("carrier", "Carrier")
    policy = res.get("policy", "N/A")
    mode = res.get("mode", "SIMULATION")
    call_id = res.get("call_id", "N/A")

    return f"""CARRIER PHONE FOLLOW-UP — Endorsement Retrieval
Policy: {policy} | Carrier: {carrier} | Phone: {phone}
Change Inquiry: {change_summary}
Dispatch Mode: {mode} (Call ID: {call_id})
Outcome: Follow-up call placed to carrier policy servicing desk. Requested endorsement documents emailed to robie@streetsmart.insurance.
Next Action: Awaiting document delivery / carrier response.

ROBIE was here"""


def main():
    parser = argparse.ArgumentParser(description="Robie Policy Change Voice AI Follow-up Dispatcher")
    parser.add_argument("--policy-number", required=True, help="Target policy number (e.g. 2021047341)")
    parser.add_argument("--carrier", required=True, help="Carrier or MGA name (e.g. 'National General', 'Merchants')")
    parser.add_argument("--insured", default="Policyholder", help="Legal Named Insured")
    parser.add_argument("--change-summary", required=True, help="Summary of change (e.g. 'Add 2015 RAM ProMaster')")
    parser.add_argument("--submission-date", default=None, help="Date change was submitted (e.g. '08/10/2026')")
    parser.add_argument("--phone", default=None, help="Direct carrier phone override (e.g. '+18004621077')")
    parser.add_argument("--instructions", default=None, help="Additional CSR notes or instructions")
    parser.add_argument("--dry-run", action="store_true",
                        help="(default behaviour) Simulate without dialing out")
    parser.add_argument("--live-voice", "--live-call", dest="live_voice", action="store_true",
                        help="ACTUALLY DIAL a real carrier. Requires explicit authorization. "
                             "Without this flag the caller always simulates.")
    parser.add_argument("--caller-id", default=None, help="Outbound caller ID override")
    add_write_gate_args(parser)

    args = parser.parse_args()

    try:
        resolve_write_gate(args)
        if args.live_voice:
            assert_write_allowed(
                "place_call",
                policy_number=args.policy_number,
                detail=f"live outbound call to {args.carrier}",
            )
    except WriteNotAuthorized as gate_err:
        logger.error("[WRITE_GATE] %s", gate_err)
        print(f"\nRefused: {gate_err}\n", file=sys.stderr)
        sys.exit(2)

    if args.live_voice:
        logger.warning("[LIVE_VOICE] Placing a REAL outbound call to %s.", args.carrier)
    else:
        logger.info("[SIMULATION] No --live-voice flag: simulating, no call will be placed.")

    res = dispatch_policy_change_call(
        policy_number=args.policy_number,
        carrier_name=args.carrier,
        insured_name=args.insured,
        change_summary=args.change_summary,
        phone_override=args.phone,
        submission_date=args.submission_date,
        custom_instructions=args.instructions,
        dry_run=not args.live_voice,
        caller_id=args.caller_id,
    )

    print("\n" + "=" * 60)
    print("📞 POLICY CHANGE CARRIER VOICE CALL DISPATCH RESULT")
    print("=" * 60)
    print(f"Success    : {res.get('success')}")
    print(f"Mode       : {res.get('mode')}")
    print(f"Carrier    : {res.get('carrier')}")
    print(f"Phone      : {res.get('phone')}")
    print(f"Policy #   : {res.get('policy')}")
    print(f"Call ID    : {res.get('call_id')}")
    if res.get("error"):
        print(f"Error      : {res.get('error')}")
        print(f"Details    : {res.get('details')}")
    print("=" * 60)

    if res.get("success"):
        print("\n📝 Formatted EZLynx Discussion Note:")
        print("-" * 60)
        print(format_ezlynx_note(res, args.change_summary))
        print("-" * 60)

    if not res.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
