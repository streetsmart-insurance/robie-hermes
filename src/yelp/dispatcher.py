"""Yelp Lead Dispatcher for BDR Chat and Alerts."""

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger("yelp_dispatcher")

BOT_SA_PATH = os.environ.get(
    "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",
    "/opt/streetsmart-hermes/.hermes/google-chat-sa.json"
)
BDR_SPACE_ID = "spaces/AAQAgMN_qrc"
BDR_WEBHOOK_URL = os.environ.get("YELP_BDR_WEBHOOK_URL", "")


def format_bdr_alert_text(lead: Dict[str, Any]) -> str:
    """Format human-friendly and urgent alert for the BDR chat."""
    name = lead.get("customer_name", "Customer")
    phone = lead.get("phone", "")
    prop_type = lead.get("property_type", "Homeowner / Property")
    loc_zip = lead.get("location_zip", "NJ")
    timing = lead.get("timing", "Standard")
    est_min = int(lead.get("estimate_min", 1000))
    est_max = int(lead.get("estimate_max", 1400))
    lead_url = lead.get("lead_url", "")

    phone_display = phone if phone else "⚠️ Check Yelp Portal"

    lines = [
        "🚨 *New Yelp Lead - Please Call*",
        f"• *Name*: {name}",
        f"• *Phone*: {phone_display}",
        f"• *Line*: {prop_type}",
        f"• *Location*: {loc_zip}",
        f"• *Timing*: {timing}",
        f"• *Benchmark Estimate*: ${est_min:,} - ${est_max:,}/yr",
    ]
    if lead_url:
        lines.append(f"• *Yelp Portal*: <{lead_url}|Open Lead on Yelp>")
    lines.append("\n👉 *Action*: Please give the customer a call right away to qualify and quote!")
    return "\n".join(lines)


def post_to_webhook(webhook_url: str, text: str) -> bool:
    """Send message to Google Chat incoming webhook."""
    if not webhook_url:
        return False
    try:
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        logger.error(f"Error posting to Google Chat webhook: {e}")
        return False


def post_via_chat_api(space_name: str, text: str) -> bool:
    """Send message via Google Chat bot service account."""
    if not os.path.exists(BOT_SA_PATH):
        logger.warning(f"Google Chat SA key not found at {BOT_SA_PATH}")
        return False
    try:
        creds = service_account.Credentials.from_service_account_file(
            BOT_SA_PATH,
            scopes=["https://www.googleapis.com/auth/chat.bot"]
        )
        chat = build("chat", "v1", credentials=creds)
        res = chat.spaces().messages().create(
            parent=space_name,
            body={"text": text}
        ).execute()
        logger.info(f"Chat API message posted to {space_name}: {res.get('name')}")
        return True
    except Exception as e:
        logger.error(f"Error posting via Chat API to {space_name}: {e}")
        return False


def dispatch_bdr_alert(lead: Dict[str, Any]) -> Dict[str, bool]:
    """Dispatch lead alert directly to the BDR chat."""
    text = format_bdr_alert_text(lead)
    results = {
        "webhook": False,
        "bdr_space_api": False,
    }

    # 1. Try BDR incoming webhook if configured
    webhook_url = os.environ.get("YELP_BDR_WEBHOOK_URL", BDR_WEBHOOK_URL)
    if webhook_url:
        results["webhook"] = post_to_webhook(webhook_url, text)

    # 2. Try posting to BDR space directly via Bot API (if bot is added to BDRs)
    results["bdr_space_api"] = post_via_chat_api(BDR_SPACE_ID, text)

    return results
