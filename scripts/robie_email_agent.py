#!/usr/bin/env python3
"""Robie Email Task Agent — Watches robie@streetsmart.insurance for incoming tasks and replies with clean results."""

import base64
import json
import logging
import os
import re
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Tuple
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

# Add robie_job_engine to path
sys.path.insert(0, "/opt/streetsmart-hermes/releases/current")
from robie_job_engine.email_guard import run_guarded_email_task
from robie_job_engine.ascend_workflow import AscendWorkflowManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("robie_email_agent")

TOKEN_PATH = "/opt/streetsmart-hermes/.hermes/robie_google_token.json"
ALLOWED_SENDERS = {"carlo@streetsmart.insurance", "jake@streetsmart.insurance"}
STATE_FILE = Path("/opt/streetsmart-hermes/.hermes/robie_processed_emails.json")
JOB_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
ATTACHMENT_DIR = Path("/tmp/robie_email_attachments")


def is_allowed_sender(sender: str) -> bool:
    clean_sender = sender.lower().strip()
    if clean_sender in ALLOWED_SENDERS:
        return True
    if clean_sender.endswith("@streetsmart.insurance") or clean_sender.endswith("@streetsmartinsurance.com"):
        return True
    return False


def get_gmail_service():
    if not os.path.exists(TOKEN_PATH):
        logger.error("Token file not found at %s", TOKEN_PATH)
        sys.exit(1)
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data)
    return build("gmail", "v1", credentials=creds)


def load_processed_ids():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_processed_ids(ids):
    STATE_FILE.write_text(json.dumps(list(ids)), encoding="utf-8")


def extract_sender_email(from_header: str) -> str:
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1).lower().strip()
    return from_header.lower().strip()


def extract_body_text(payload: dict) -> str:
    """Recursively extract plain text or HTML body from Gmail payload."""
    chunks = []
    mime_type = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")

    if data and mime_type.startswith("text/"):
        try:
            text = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            if mime_type == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text)
            chunks.append(text.strip())
        except Exception:
            pass

    for part in payload.get("parts", []):
        sub_text = extract_body_text(part)
        if sub_text:
            chunks.append(sub_text)

    return "\n\n".join(chunks).strip()


def download_attachments(service, msg_id: str, payload: dict) -> List[Tuple[str, str]]:
    """Download attachments from message payload and return list of (filename, file_path)."""
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = []

    def _recurse_parts(parts):
        for part in parts:
            filename = part.get("filename")
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")

            if filename and (attachment_id or body.get("data")):
                try:
                    if attachment_id:
                        att = (
                            service.users()
                            .messages()
                            .attachments()
                            .get(userId="me", messageId=msg_id, id=attachment_id)
                            .execute()
                        )
                        file_data = base64.urlsafe_b64decode(att.get("data", "") + "===")
                    else:
                        file_data = base64.urlsafe_b64decode(body.get("data", "") + "===")

                    safe_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)
                    dest_path = ATTACHMENT_DIR / f"{msg_id}_{safe_filename}"
                    with open(dest_path, "wb") as f:
                        f.write(file_data)

                    downloaded.append((filename, str(dest_path)))
                    logger.info("Saved attachment %s to %s", filename, dest_path)
                except Exception as exc:
                    logger.warning("Failed downloading attachment %s: %s", filename, exc)

            if part.get("parts"):
                _recurse_parts(part.get("parts", []))

    _recurse_parts(payload.get("parts", []))
    return downloaded


def clean_hermes_output(raw_output: str) -> str:
    """Extracts ONLY the clean assistant response from CLI stdout, stripping all debug/tool logs."""
    box_match = re.search(r"╭─+ ⚕ Hermes ─+╮\s*\n(.*?)\n╰─+╯", raw_output, re.DOTALL)
    if box_match:
        return box_match.group(1).strip()

    lines = []
    in_footer = False
    for line in raw_output.split("\n"):
        if "Resume this session with:" in line or line.startswith("Session:") or line.startswith("Duration:"):
            in_footer = True
            continue
        if in_footer:
            continue
        if any(marker in line for marker in ["┊ 💻", "┊ 📚", "┊ 🔎", "┊ 📖", "┊ ✍️", "preparing ", "review diff", "Query:"]):
            continue
        if line.strip().startswith("─") or line.strip().startswith("╭") or line.strip().startswith("╰"):
            continue
        lines.append(line)

    cleaned = "\n".join(lines).strip()
    return cleaned or raw_output.strip()


def run_agent_task(prompt: str) -> str:
    """Executes prompt via Hermes Agent on the VM and returns clean response."""
    env = os.environ.copy()
    env["HOME"] = "/opt/streetsmart-hermes"
    env["HERMES_HOME"] = "/opt/streetsmart-hermes/.hermes"
    env["ROBIE_ENV"] = "PRODUCTION"
    env["ROBIE_ASCEND_API_ENABLED"] = "true"
    env["ROBIE_ASCEND_API_PRODUCTION_ENABLED"] = "true"
    env["ROBIE_ASCEND_API_KEY"] = "Yoo9IziU9MBws0GuzaXww4t1XWqrxrjynaGE0-vltUo"
    env["ROBIE_ASCEND_API_BASE_URL"] = "https://api.useascend.com/v1"

    cmd = [
        "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python",
        "-m", "hermes_cli.main",
        "chat",
        "-q", prompt
    ]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
            env=env,
            cwd="/opt/streetsmart-hermes"
        )
        return clean_hermes_output(res.stdout)
    except Exception as exc:
        logger.error("Error executing Hermes agent task: %s", exc)
        return f"Error executing task: {exc}"


def process_inbox():
    service = get_gmail_service()
    processed_ids = load_processed_ids()

    query = "is:unread is:inbox"
    res = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
    messages = res.get("messages", [])

    if not messages:
        return

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if msg_id in processed_ids:
            continue

        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}

        from_hdr = headers.get("from", "")
        sender = extract_sender_email(from_hdr)
        subject = headers.get("subject", "No Subject")
        thread_id = msg.get("threadId", msg_id)

        if not is_allowed_sender(sender):
            logger.info("Skipping email from unauthorized sender: %s", sender)
            continue

        payload = msg.get("payload", {})
        body = extract_body_text(payload) or msg.get("snippet", "")
        attachments = download_attachments(service, msg_id, payload)

        logger.info("📩 Processing task email from %s: '%s' (%d attachments)", sender, subject, len(attachments))

        # Check if email is an Ascend / quote financing agreement request
        combined_text = f"{subject}\n{body}".lower()
        is_ascend_request = any(
            k in combined_text
            for k in ["ascend", "agreement", "finance agreement", "financing agreement", "payment agreement", "quote", "bind"]
        )

        response_text = ""
        if is_ascend_request:
            try:
                pdf_path = next((p for _, p in attachments if p.lower().endswith(".pdf")), None)
                quote_source = Path(pdf_path) if pdf_path else body
                
                manager = AscendWorkflowManager()
                result = manager.process_quote_request(
                    raw_text_or_pdf=quote_source,
                    user_instruction=body,
                    sender_email=sender,
                )
                if result.status in ("COMPLETED", "NEEDS_CLARIFICATION") and result.reply_email_body:
                    response_text = result.reply_email_body
                    logger.info("Handled Ascend workflow deterministically (status=%s)", result.status)
            except Exception as exc:
                logger.warning("Deterministic Ascend workflow error, falling back to agent: %s", exc)

        if not response_text:
            attachment_lines = ""
            if attachments:
                attachment_lines = "\n\nAttached Files (saved locally):\n" + "\n".join(
                    f"- Filename: {name} (Local path: {path})" for name, path in attachments
                )

            task_prompt = (
                f"You are Robie, the autonomous insurance operations AI agent at StreetSmart Insurance.\n"
                f"You received an incoming email from {sender}.\n"
                f"Subject: {subject}\n\n"
                f"Email Body Content:\n{body}\n"
                f"{attachment_lines}\n\n"
                f"Instructions:\n"
                f"1. If the user is asking to create an Ascend payment agreement or finance agreement (or sending an insurance quote for agreement generation):\n"
                f"   - Use the 'ascend-api-create-program' skill or the Ascend API Python tools (`robie_job_engine.ascend_workflow.AscendWorkflowManager` or `QuoteExtractor`).\n"
                f"   - Check if agency fee, commission rate, surplus lines tax, and terrorism coverage are clear from the email body or attached PDF quote.\n"
                f"   - If any of those 4 parameters are missing or ambiguous (e.g. quote has options with/without terrorism), ask {sender} to clarify what they want.\n"
                f"   - Once clear or if already specified, generate the program via Ascend API, post the checkout link discussion note to EZLynx, and provide {sender} the Ascend checkout link and quote breakdown.\n"
                f"2. For any other request, execute the required insurance operations skill and assist thoroughly.\n"
                f"3. Write a professional, concise, polished email response directly addressing {sender}."
            )

            response_text = run_guarded_email_task(
                db_path=JOB_DB,
                gmail_message_id=msg_id,
                prompt=task_prompt,
                run_agent=run_agent_task,
            )

        # Send clean reply
        reply_msg = MIMEText(response_text)
        reply_msg["to"] = sender
        reply_msg["from"] = "Robie AI <robie@streetsmart.insurance>"
        reply_msg["subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
        reply_msg["In-Reply-To"] = headers.get("message-id", "")
        reply_msg["References"] = headers.get("message-id", "")

        raw_payload = base64.urlsafe_b64encode(reply_msg.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me",
            body={"raw": raw_payload, "threadId": thread_id}
        ).execute()

        # Mark as read
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()

        processed_ids.add(msg_id)
        save_processed_ids(processed_ids)
        logger.info("✓ Clean reply sent to %s on thread %s", sender, thread_id)


if __name__ == "__main__":
    process_inbox()
