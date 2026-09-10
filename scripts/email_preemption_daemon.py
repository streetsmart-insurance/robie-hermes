import sys
sys.path.insert(0, "/opt/renewal-automation-system")
import logging
import time
import requests
import json
from google.cloud import secretmanager
from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient

logging.basicConfig(
    filename="/tmp/email_preemption_daemon.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("preemption_daemon")

def get_voice_api_key():
    client = secretmanager.SecretManagerServiceClient()
    name = "projects/streetsmart-hermes-poc/secrets/carrier_voice_api_key/versions/latest"
    resp = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode("UTF-8").strip()

def stop_bland_call(call_id, api_key):
    headers = {
        "authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    url = f"https://api.bland.ai/v1/calls/{call_id}/stop"
    r = requests.post(url, headers=headers, timeout=15)
    logger.info(f"Stop call {call_id}: HTTP {r.status_code} - {r.text}")
    return r.status_code in (200, 201)

def main():
    logger.info("Starting email preemption continuous watcher...")
    api_key = get_voice_api_key()
    gc = GmailRenewalClient()
    ez = EZLynxApiClient()

    # Monitored calls: (call_id, policy_number, carrier_name, underwriter_email, applicant_id, discussion_title)
    monitored = [
        {
            "call_id": "9c433ccc-3fd4-4dab-849e-1d246942d528",
            "policy_number": "EZXS3251600",
            "carrier_name": "JIMCOR MGA",
            "insured_name": "Seacrest Sales & Marketing Corporation",
            "underwriter_email": "ARivera@jimcor.com",
            "applicant_id": "217163055",
            "discussion_title": "Excess Liability Policy Change Request - increase umbrella coverage to 5 mil",
            "start_time": time.time() - 300
        }
    ]

    for iteration in range(120): # Monitor up to 1 hour (every 30s)
        time.sleep(30)
        remaining = []
        for item in monitored:
            call_id = item["call_id"]
            # Check call status
            headers = {"Authorization": api_key, "User-Agent": "Mozilla/5.0"}
            c_resp = requests.get(f"https://api.bland.ai/v1/calls/{call_id}", headers=headers, timeout=10)
            c_data = c_resp.json() if c_resp.text else {}
            c_status = c_data.get("status")

            if c_status in ("completed", "canceled", "failed", "no-answer"):
                logger.info(f"Call {call_id} ended with status: {c_status}")
                continue

            # Check for incoming email from underwriter
            q = f"from:{item['underwriter_email']}"
            res = gc.service.users().messages().list(userId="me", q=q).execute()
            messages = res.get("messages", [])
            reply_found = None
            for m in messages:
                msg = gc.service.users().messages().get(
                    userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_hdr = hdrs.get("from", "")
                if "robie@streetsmart.insurance" not in from_hdr.lower():
                    msg_time = int(msg.get("internalDate", 0)) / 1000.0
                    if msg_time >= item["start_time"]:
                        reply_found = {
                            "from": from_hdr,
                            "subject": hdrs.get("subject", ""),
                            "date": hdrs.get("date", ""),
                            "snippet": msg.get("snippet", "")
                        }
                        break

            if reply_found:
                logger.warning(f"Carrier email reply detected! Killing call {call_id}...")
                stop_bland_call(call_id, api_key)
                kill_note = f"""=== [AUTONOMOUS CALL CANCELLED - EMAIL REPLY RECEIVED] ===
Policy: #{item['policy_number']} ({item['carrier_name']})
Insured: {item['insured_name']}
Action: Bland AI Call ID {call_id} killed immediately because carrier underwriter replied via email before call connection.
From: {reply_found['from']}
Subject: {reply_found['subject']}
Date: {reply_found['date']}
Snippet: {reply_found['snippet'][:200]}

ROBIE was here"""
                ez.add_note_to_discussion(
                    applicant_id=item["applicant_id"],
                    discussion_title=item["discussion_title"],
                    note_text=kill_note,
                    policy_number=item["policy_number"],
                    carrier_name=item["carrier_name"]
                )
                logger.info(f"Killed call {call_id} and posted note to EZLynx.")
            else:
                remaining.append(item)

        monitored = remaining
        if not monitored:
            logger.info("All monitored calls finished or stopped.")
            break

if __name__ == "__main__":
    main()
