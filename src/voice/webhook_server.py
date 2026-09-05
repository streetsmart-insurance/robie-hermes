"""
Post-Call Webhook Receiver for Autonomous Carrier Voice Engine.
Receives completed call transcripts and recordings from Bland AI / Retell AI,
auto-threads notes into EZLynx discussion cards, and notifies assigned CSRs.
Built using standard library http.server for zero external dependencies.
"""

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any

from src.database.models import PolicyRenewal, RenewalStatus
from src.database.session import SessionLocal
from src.ezlynx.api_client import EZLynxApiClient
from src.email_outreach.gmail_client import GmailRenewalClient

logger = logging.getLogger("voice_webhook_server")


def handle_completed_call(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Core business logic to process completed call payload:
    1. Formats standardized audit note
    2. Auto-threads note into EZLynx discussion card
    3. Updates renewals.db
    4. Sends CSR summary email
    """
    call_id = data.get("call_id") or data.get("id", "UNKNOWN_CALL")
    recording_url = data.get("recording_url") or data.get("audio_url", "N/A")
    summary = data.get("summary") or data.get("call_summary", "Call completed.")
    transcript = data.get("concatenated_transcript") or data.get("transcript", "")
    metadata = data.get("metadata", {})

    policy_number = metadata.get("policy_number") or data.get("policy_number")
    carrier_name = metadata.get("carrier_name") or data.get("carrier_name", "Carrier Underwriting")
    insured_name = metadata.get("insured_name") or data.get("insured_name", "Insured Account")
    applicant_id = metadata.get("applicant_id") or data.get("applicant_id")
    csr_email = metadata.get("assigned_csr_email") or data.get("assigned_csr_email", "carlo@streetsmart.insurance")
    lob = metadata.get("line_of_business", "Commercial")

    logger.info(f"Processing completed call {call_id} for Policy #{policy_number} ({insured_name})")

    # 1. Look up policy record in DB if applicant_id missing
    db = SessionLocal()
    try:
        pol_record = None
        if policy_number:
            pol_record = db.query(PolicyRenewal).filter(PolicyRenewal.policy_number.ilike(f"%{policy_number.strip()}%")).first()
        if pol_record:
            if not applicant_id:
                applicant_id = pol_record.applicant_id
            if not carrier_name or carrier_name == "Carrier Underwriting":
                carrier_name = pol_record.carrier_name
            if not insured_name or insured_name == "Insured Account":
                insured_name = pol_record.insured_name
            lob = pol_record.line_of_business or lob

            # Check if quote was issued
            if any(k in summary.lower() for k in ["quote issued", "terms released", "quoted", "available in portal"]):
                pol_record.status = RenewalStatus.QUOTE_RECEIVED

            db.commit()
    finally:
        db.close()

    # 2. Format standardized audit note per AGENTS.md mandate
    note_header = f"Policy: #{policy_number} ({lob} - {carrier_name})"
    note_body = f"""{note_header}
Autonomous Carrier Phone Outreach Completed:
- Result: Call finished successfully
- Summary: {summary}
- Audio Recording: {recording_url}

Robie was here"""

    # 3. Post note directly into EZLynx discussion card
    ezlynx_posted = False
    note_id = None
    if applicant_id:
        try:
            client = EZLynxApiClient()
            res = client.add_note_to_discussion(
                applicant_id=int(applicant_id),
                discussion_title=f"Renewal Manual {lob} | {policy_number} {carrier_name}",
                note_text=note_body,
                policy_number=policy_number,
                line_of_business=lob,
                carrier_name=carrier_name,
            )
            ezlynx_posted = True
            note_id = res.get("note_id")
            logger.info(f"Successfully posted voice call note {note_id} to EZLynx for Applicant {applicant_id}")
        except Exception as e:
            logger.error(f"Failed to post voice note to EZLynx: {e}")

    # 4. Send CSR notification email
    email_sent = False
    try:
        gmail = GmailRenewalClient()
        email_subj = f"Carrier Call Report: {carrier_name} - Pol #{policy_number} ({insured_name})"
        email_body = f"""Hi there,

Robie has completed the outbound follow-up call with {carrier_name} underwriting.

- **Insured Account:** {insured_name}
- **Policy Number:** #{policy_number} ({lob})
- **Carrier:** {carrier_name}
- **Summary:** {summary}
- **Audio Recording:** {recording_url}

This activity has been posted directly to the EZLynx Discussion Card for this policy.

Transcript excerpt:
{transcript[:600] + ('...' if len(transcript) > 600 else '')}

Best regards,
Robie
StreetSmart Insurance Operations Engine
"""
        gmail.send_email(to_email=csr_email, subject=email_subj, body_text=email_body)
        email_sent = True
    except Exception as e:
        logger.error(f"Failed to send CSR email for call {call_id}: {e}")

    return {
        "call_id": call_id,
        "policy_number": policy_number,
        "ezlynx_posted": ezlynx_posted,
        "note_id": note_id,
        "email_sent": email_sent,
    }


class VoiceWebhookRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/webhook/voice/call-completed"):
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid JSON", "details": str(e)}).encode("utf-8"))
                return

            call_data = payload.get("call", payload)
            # Run processing in background thread
            threading.Thread(target=handle_completed_call, args=(call_data,), daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "received", "call_id": call_data.get("call_id")}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "robie_voice_webhook"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_webhook_server(host: str = "0.0.0.0", port: int = 8088):
    server = HTTPServer((host, port), VoiceWebhookRequestHandler)
    logger.info(f"Starting Robie Voice Webhook server on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_webhook_server()
