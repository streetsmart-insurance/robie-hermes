"""
EZLynx Label & Note Call Dispatcher.

Enables CSRs and Account Managers to trigger autonomous calls directly from EZLynx:
1. CSR adds a Note or Discussion with label/title 'robie call'.
2. Note specifies 'Who to call' (phone number/carrier) and 'What to say' (instructions).
3. Robie parses the instructions, dispatches the call via Bland AI from +1 (732) 298-6745.
4. When complete, Robie automatically posts the call transcript, recording link, and summary
   back into the applicant's EZLynx discussion card.
"""

import logging
import re
from typing import Optional, Dict, Any, List

from src.ezlynx.api_client import EZLynxApiClient
from src.voice.voice_client import CarrierVoiceClient
from src.voice.context_hydrator import CallingDossier

logger = logging.getLogger("ezlynx_label_dispatcher")

ROBIE_LABEL_TRIGGERS = ["robie call", "robie_call", "call robie", "robie: call", "[robie call]"]


def parse_call_note_instructions(note_text: str) -> Dict[str, Any]:
    """
    Parses a note written by a CSR in EZLynx containing call instructions.
    
    Expected flexible formats:
    - Who to call: Hartford (800-555-1234)
    - What to say: Inquire about renewal quote for policy 12345
    - Phone: 800-555-1234
    - Contact: Jane Doe
    """
    clean_text = note_text.strip()
    result = {
        "is_robie_call": False,
        "phone_number": None,
        "target_name": None,
        "policy_number": None,
        "instructions": "",
    }

    # Check for trigger tag/keyword
    lower_text = clean_text.lower()
    if any(trigger in lower_text for trigger in ROBIE_LABEL_TRIGGERS):
        result["is_robie_call"] = True

    # 1. Extract Phone Number
    # Match standard US phone formats: (xxx) xxx-xxxx, xxx-xxx-xxxx, 1xxxxxxxxxx
    phone_match = re.search(r"(?:who to call|phone|call|dial|tel|number)?[:\s]*(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b", clean_text, re.IGNORECASE)
    if phone_match:
        raw_phone = phone_match.group(1).strip()
        digits = re.sub(r"\D", "", raw_phone)
        if len(digits) == 10:
            result["phone_number"] = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            result["phone_number"] = f"+{digits}"

    # 2. Extract Who To Call (Entity / Carrier / Contact)
    who_match = re.search(r"(?:who to call|contact|carrier|target)[:\s]+([^;\n\r]+)", clean_text, re.IGNORECASE)
    if who_match:
        candidate_who = who_match.group(1).strip()
        # Clean out any trailing phone number if captured in same line
        candidate_who = re.sub(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", "", candidate_who).strip()
        result["target_name"] = candidate_who.strip(" -:,")

    # 3. Extract Policy Number
    pol_match = re.search(r"(?:policy|pol|policy number|pol#|policy#)[:\s#]+([A-Z0-9\-]{5,})", clean_text, re.IGNORECASE)
    if pol_match:
        result["policy_number"] = pol_match.group(1).strip()

    # 4. Extract What To Say / Instructions
    say_match = re.search(r"(?:what to say|instructions|say|message|notes|details)[:\s]+([\s\S]+)", clean_text, re.IGNORECASE)
    if say_match:
        instructions = say_match.group(1).strip()
        # Remove signature lines if present
        instructions = re.sub(r"(?:Robie was here|Thanks|Sincerely).*$", "", instructions, flags=re.IGNORECASE | re.DOTALL).strip()
        result["instructions"] = instructions
    else:
        # If no explicit "what to say:" label, use the remaining text
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
        filtered_lines = []
        for line in lines:
            line_lower = line.lower()
            if any(t in line_lower for t in ROBIE_LABEL_TRIGGERS):
                continue
            if line_lower.startswith(("who to call:", "phone:", "contact:", "policy:")):
                continue
            filtered_lines.append(line)
        result["instructions"] = " ".join(filtered_lines).strip()

    return result


KNOWN_CARRIER_PHONES = {
    "the hartford": "+18005551234",
    "hartford": "+18005551234",
    "travelers": "+18002386225",
    "coterie": "+18555673421",
    "coterie insurance": "+18555673421",
    "progressive": "+18008765581",
    "tapco": "+18003345579",
    "tapco underwriters": "+18003345579",
    "chubb": "+18002524670",
    "chubb group": "+18002524670",
    "amtrust": "+18775287878",
    "cna": "+18002622000",
    "cna surety": "+18002622000",
    "liberty mutual": "+18003440197",
    "employers": "+18886826671",
    "guard": "+18006732265",
    "berkshire hathaway guard": "+18006732265",
    "bhhc": "+18884958949",
    "berkshire hathaway": "+18884958949",
    "rps": "+18665958405",
    "risk placement services": "+18665958405",
    "amwins": "+18002213824",
    "jimcor": "+18006440333",
    "specialty coverage": "+18002422200",
    "new england excess": "+18005484301",
    "markel": "+18004311270",
}


def lookup_known_carrier_phone(carrier_name: Optional[str]) -> Optional[str]:
    """Resolves carrier phone number from known directory or hydrator."""
    if not carrier_name:
        return None
    clean_name = carrier_name.lower().strip()
    for key, phone in KNOWN_CARRIER_PHONES.items():
        if key in clean_name or clean_name in key:
            return phone
    return None


class EZLynxLabelCallDispatcher:
    """Dispatches calls based on EZLynx notes/discussions tagged 'robie call'."""

    def __init__(
        self,
        ezlynx_client: Optional[EZLynxApiClient] = None,
        voice_client: Optional[CarrierVoiceClient] = None,
    ):
        self.ezlynx = ezlynx_client or EZLynxApiClient()
        self.voice = voice_client or CarrierVoiceClient()

    def process_applicant_notes_for_calls(
        self,
        applicant_id: str,
        dry_run: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetches discussions/notes for an applicant, scans for 'robie call' instructions,
        resolves missing carrier phone numbers from policy or carrier directory,
        initiates the calls, and posts confirmation or clarification notes back.
        """
        results = []
        discussions = self.ezlynx.get_applicant_discussions(applicant_id)
        if not discussions:
            logger.info(f"No discussions found for applicant {applicant_id}")
            return results

        # Fetch applicant profile for context
        app_res = self.ezlynx.get_applicant(applicant_id)
        app_data = app_res.get("applicant", {}) if app_res.get("status") == "success" else {}
        insured_name = (
            app_data.get("BusinessName")
            or f"{app_data.get('FirstName', '')} {app_data.get('LastName', '')}".strip()
            or f"Applicant #{applicant_id}"
        )

        # Lazy cache applicant policies to avoid duplicate API calls
        applicant_policies = None

        for disc in discussions:
            title = disc.get("title", "")
            note_obj = disc.get("discussionNote", {})
            note_text = note_obj.get("noteText", "") or disc.get("description", "")
            combined_text = f"{title}\n{note_text}"

            parsed = parse_call_note_instructions(combined_text)
            if not parsed["is_robie_call"]:
                continue

            # Guard: Prevent re-triggering if the latest note was already posted by Robie
            last_author = note_obj.get("createdByName") or disc.get("lastModifiedByName", "")
            if "robie" in last_author.lower() or "[ROBIE" in note_text:
                logger.debug(f"Skipping discussion '{title}': latest activity already handled by Robie.")
                continue

            # 1. Resolve Policy Number if missing in note body
            policy_num = parsed["policy_number"]
            if not policy_num:
                # Attempt to extract from discussion title (e.g. "... | PWC1239278 Associated Specialty")
                pol_in_title = re.search(r"\|\s*([A-Z0-9\-]{5,})\b", title, re.IGNORECASE)
                if pol_in_title:
                    policy_num = pol_in_title.group(1).strip()
                else:
                    # Look up active policies on applicant
                    if applicant_policies is None:
                        applicant_policies = self.ezlynx.get_applicant_policies(applicant_id)
                    if applicant_policies and isinstance(applicant_policies, list):
                        policy_num = applicant_policies[0].get("policyNumber") or applicant_policies[0].get("PolicyNumber")

            # 2. Resolve Carrier Name if missing
            target_carrier = parsed["target_name"]
            if not target_carrier or target_carrier == "Carrier Representative":
                # Check policy directory or applicant policies
                if applicant_policies is None:
                    applicant_policies = self.ezlynx.get_applicant_policies(applicant_id)
                if applicant_policies and isinstance(applicant_policies, list):
                    for pol in applicant_policies:
                        p_num = pol.get("policyNumber") or pol.get("PolicyNumber")
                        if policy_num and p_num == policy_num:
                            target_carrier = pol.get("carrierName") or pol.get("CarrierName") or pol.get("companyName")
                            break
                    if not target_carrier and applicant_policies:
                        target_carrier = applicant_policies[0].get("carrierName") or applicant_policies[0].get("CarrierName")

            target_carrier = target_carrier or "Carrier Representative"

            # 3. Resolve Phone Number from carrier knowledge base / directory if missing
            phone = parsed["phone_number"]
            if not phone:
                phone = lookup_known_carrier_phone(target_carrier)
                if phone:
                    logger.info(f"Resolved phone {phone} for carrier '{target_carrier}' from directory.")

            instructions = parsed["instructions"] or "Inquire regarding policy status and quote release."
            safe_pol_num = policy_num or "N/A"

            # 4. If phone is STILL missing, post a polite clarification note directly via API (avoiding Playwright)
            if not phone:
                logger.warning(f"Applicant {applicant_id}: 'robie call' requested for '{target_carrier}', but no phone number found.")
                clarification_note = (
                    f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
                    f"⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]\n"
                    f"Robie received your request to call regarding this account, but could not determine "
                    f"the carrier phone number for '{target_carrier}'.\n\n"
                    f"To trigger this call, please reply to this card with the phone number:\n"
                    f"• Example: \"Phone: 800-555-1234\"\n"
                    f"• Or provide the underwriter direct contact info.\n\n"
                    f"Robie will automatically place the call once the number is provided."
                )
                self.ezlynx.add_note_to_discussion(
                    applicant_id=applicant_id,
                    discussion_title=title,
                    note_text=clarification_note,
                    policy_number=policy_num,
                    carrier_name=target_carrier,
                )
                results.append({
                    "applicant_id": applicant_id,
                    "discussion_title": title,
                    "target": target_carrier,
                    "status": "CLARIFICATION_NEEDED",
                    "reason": "MISSING_PHONE_NUMBER",
                })
                continue

            logger.info(
                f"[ROBIE CALL DETECTED] Applicant: {applicant_id} ({insured_name}) | "
                f"Target: {target_carrier} ({phone}) | Policy: {safe_pol_num}"
            )

            dossier = CallingDossier(
                policy_number=safe_pol_num,
                insured_name=insured_name,
                carrier_name=target_carrier,
                carrier_phone=phone,
                line_of_business="Commercial Lines",
                applicant_id=applicant_id,
                custom_instructions=instructions,
            )

            # Build call prompt
            call_prompt = self.voice.build_call_prompt(
                dossier=dossier,
                custom_instructions=f"SPECIFIC CSR INSTRUCTIONS: {instructions}",
            )

            # Dispatch call
            call_result = self.voice.dispatch_call(
                dossier=dossier,
                dry_run=dry_run,
            )

            call_id = call_result.get("call_id", "sim_call_001")
            status = call_result.get("status", "DISPATCHED")

            # Post immediate acknowledgement note back to EZLynx discussion card
            ack_note = (
                f"Policy: #{safe_pol_num} ({target_carrier})\n\n"
                f"🤖 [ROBIE AUTONOMOUS CALL DISPATCHED]\n"
                f"Robie has placed an outbound call to {target_carrier} at {phone}.\n"
                f"Caller ID: {self.voice.from_phone or '+1 (732) 298-6745'}\n"
                f"Call ID: {call_id}\n"
                f"Instructions: \"{instructions}\"\n\n"
                f"When the call concludes, full audio recording and transcript will be posted here."
            )

            self.ezlynx.add_note_to_discussion(
                applicant_id=applicant_id,
                discussion_title=title,
                note_text=ack_note,
                policy_number=policy_num,
                carrier_name=target_carrier,
            )

            results.append({
                "applicant_id": applicant_id,
                "discussion_title": title,
                "phone": phone,
                "target": target_carrier,
                "call_id": call_id,
                "status": status,
            })

        return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scan EZLynx for 'robie call' labeled notes and dispatch calls.")
    parser.add_argument("--applicant-id", type=str, help="Specific applicant ID to scan")
    parser.add_argument("--dry-run", action="store_true", help="Simulate call dispatch without hitting Bland AI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    dispatcher = EZLynxLabelCallDispatcher()
    if args.applicant_id:
        res = dispatcher.process_applicant_notes_for_calls(args.applicant_id, dry_run=args.dry_run)
        print(f"Processed {len(res)} calls for applicant {args.applicant_id}: {res}")
    else:
        print("Please provide --applicant-id to scan.")


if __name__ == "__main__":
    main()
