import base64
import re
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


TOKEN = "/opt/streetsmart-hermes/.hermes/robie_google_token.json"
creds = Credentials.from_authorized_user_file(TOKEN)
service = build("gmail", "v1", credentials=creds, cache_discovery=False)
result = service.users().messages().list(userId="me", q="newer_than:1d", maxResults=100).execute()
cutoff = datetime.now(timezone.utc) - timedelta(minutes=45)
printed_body = False


def body_text(payload: dict) -> str:
    chunks = []
    data = payload.get("body", {}).get("data")
    if data:
        chunks.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace"))
    for part in payload.get("parts", []):
        chunks.append(body_text(part))
    return "\n".join(chunks)


for meta in result.get("messages", []):
    message = service.users().messages().get(
        userId="me", id=meta["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]
    ).execute()
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, timezone.utc)
    if received < cutoff:
        continue
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in message.get("payload", {}).get("headers", [])
    }
    safe = {
        "received": received.isoformat(),
        "from": re.sub(r"\d{4,}", "[REDACTED]", headers.get("from", "")),
        "subject": re.sub(r"\d{4,}", "[REDACTED]", headers.get("subject", "")),
    }
    print(safe)
    if not printed_body and "ezlynx" in headers.get("from", "").lower():
        full = service.users().messages().get(userId="me", id=meta["id"], format="full").execute()
        masked = re.sub(r"\d", "X", body_text(full.get("payload", {})))
        print(masked[:4_000])
        printed_body = True
