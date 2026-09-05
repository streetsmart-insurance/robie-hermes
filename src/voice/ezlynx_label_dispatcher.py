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
        initiates the calls, and posts confirmation back.
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

        for disc in discussions:
            title = disc.get("title", "")
            note_obj = disc.get("discussionNote", {})
            note_text = note_obj.get("noteText", "") or disc.get("description", "")
            combined_text = f"{title}\n{note_text}"

            parsed = parse_call_note_instructions(combined_text)
            if not parsed["is_robie_call"]:
                continue

            phone = parsed["phone_number"]
            if not phone:
                logger.warning(f"Applicant {applicant_id}: 'robie call' found but no phone number specified.")
                continue

            target_carrier = parsed["target_name"] or "Carrier Representative"
            policy_num = parsed["policy_number"] or "N/A"
            instructions = parsed["instructions"] or "Inquire regarding policy status and quote release."

            logger.info(
                f"[ROBIE CALL DETECTED] Applicant: {applicant_id} ({insured_name}) | "
                f"Target: {target_carrier} ({phone}) | Policy: {policy_num}"
            )

            dossier = CallingDossier(
                policy_number=policy_num,
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
                f"Policy: #{policy_num} ({target_carrier})\n\n"
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
                policy_number=policy_num if policy_num != "N/A" else None,
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
