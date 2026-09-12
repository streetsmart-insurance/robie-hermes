#!/usr/bin/env python3
"""Robie Email Task Agent — Watches robie@streetsmart.insurance for incoming tasks and replies with clean results."""

import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Tuple
from googleapiclient.discovery import build
import dataclasses
from google.oauth2.credentials import Credentials

# Add robie_job_engine to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robie_job_engine.email_guard import EmailTaskPending, run_guarded_email_task
from robie_job_engine.ascend_workflow import AscendWorkflowManager
from robie_job_engine.quote_extractor import ExtractedQuote, strip_email_reply_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("robie_email_agent")

_release_root = next((p for p in Path(__file__).resolve().parents if p.name in {"streetsmart-hermes", "streetsmart-hermes-test"} and p.parent == Path("/opt")), None)
if _release_root is None:
    raise RuntimeError("Email launcher must run from an installed Test or Production release")
OPT_ROOT = Path(os.environ.get("ROBIE_OPT_ROOT", str(_release_root)))
if OPT_ROOT != _release_root:
    raise RuntimeError("Email environment and installed release root disagree")
_EXPECTED_ENV = "TEST" if OPT_ROOT.name.endswith("-test") else "PRODUCTION"
if os.environ.get("ROBIE_ENV", _EXPECTED_ENV).upper() != _EXPECTED_ENV:
    raise RuntimeError("Email environment and installed release disagree")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(OPT_ROOT / ".hermes")))
if HERMES_HOME != OPT_ROOT / ".hermes":
    raise RuntimeError("Email Hermes home is outside its installed environment")
TOKEN_PATH = str(HERMES_HOME / "robie_google_token.json")
ALLOWED_SENDERS = {"carlo@streetsmart.insurance", "jake@streetsmart.insurance"}
STATE_FILE = HERMES_HOME / "robie_processed_emails.json"
SESSION_FILE = Path(os.environ.get("ROBIE_SESSION_FILE", str(HERMES_HOME / "robie_ascend_sessions.json")))
JOB_DB = os.environ.get("ROBIE_JOB_DB", str(OPT_ROOT / "robie-job-engine/data/jobs.db"))
if not Path(JOB_DB).resolve().is_relative_to(OPT_ROOT.resolve()):
    raise RuntimeError("Email job database is outside its installed environment")
ATTACHMENT_DIR = Path("/tmp/robie_email_attachments")


def load_ascend_sessions() -> dict:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read ascend sessions: %s", e)
            return {}
    return {}


def save_ascend_sessions(sessions: dict):
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed saving ascend sessions: %s", exc)


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


def run_agent_task(prompt: str, job_id: str = "", db_path: str = "") -> str:
    """Executes prompt via Hermes Agent on the VM and returns clean response."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(OPT_ROOT)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["ROBIE_ENV"] = _EXPECTED_ENV
    if job_id:
        env["ROBIE_JOB_ID"] = job_id
        env["ROBIE_CURRENT_JOB_ID"] = job_id
        env["JOB_ID"] = job_id
    if db_path:
        env["ROBIE_JOB_DB"] = db_path

    from robie_job_engine.email_agent_runner import run_scripted_email
    return run_scripted_email(prompt, env=env, home=HERMES_HOME, cwd=OPT_ROOT,
                              job_id=job_id, db_path=db_path)


def run_email_job(prompt, job_id, db_path, *, sender, subject, body, attachments, thread_id):
    """Bound both generic and finance execution to one cancellable process group."""
    env = dict(os.environ, ROBIE_CURRENT_JOB_ID=job_id, ROBIE_JOB_ID=job_id, JOB_ID=job_id, ROBIE_JOB_DB=db_path)
    request = dict(sender=sender, subject=subject, body=body, attachments=attachments,
                   thread_id=thread_id, job_id=job_id, db_path=db_path, context_prompt=prompt)
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--execute-job'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True)
    try:
        stdout, _ = child.communicate(json.dumps(request), timeout=930)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate(timeout=10)
        return 'ROBIE_OUTCOME_UNKNOWN: The task timed out and its execution was stopped. Check the destination before retrying.'
    # The isolated task group may still contain descendants after its leader exits.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if child.returncode:
        return 'ROBIE_OUTCOME_UNKNOWN: The task process exited unexpectedly. Check the destination before retrying.'
    try:
        response = json.loads(stdout)['response']
        if not isinstance(response, str):
            raise ValueError('Response must be text')
        return response
    except (ValueError, KeyError, TypeError):
        return 'ROBIE_OUTCOME_UNKNOWN: The task returned no usable receipt. Check the destination before retrying.'


def execute_email_work(sender, subject, body, attachments, thread_id, job_id, db_path, context_prompt=""):
    """Execute either route only after the Job Engine has claimed this email."""
    from robie_job_engine.store import JobStore
    store = JobStore(db_path)
    # Check active Ascend sessions for this thread
    sessions = load_ascend_sessions()
    existing_session = sessions.get(thread_id)

    # Check if email is an Ascend request or continuation of an active session
    combined_text = f"{subject}\n{body}".lower()
    is_ascend_request = bool(existing_session and not existing_session.get("closed")) or any(
        k in combined_text
        for k in ["ascend", "finance agreement", "financing agreement", "payment agreement", "premium finance"]
    )

    response_text = ""
    if is_ascend_request:
        store.checkpoint(job_id, 'email_route', {'route': 'finance'})
        try:
            manager = AscendWorkflowManager()
            pdf_path = next((p for _, p in attachments if p.lower().endswith(".pdf")), None)

            # Extract EZLynx applicant_id if present in email text or prior session
            applicant_id = None
            ezlynx_match = re.search(r"ezlynx\.com/web/account/(\d+)", f"{subject}\n{body}")
            if ezlynx_match:
                applicant_id = ezlynx_match.group(1)
            elif existing_session and existing_session.get("applicant_id"):
                applicant_id = existing_session.get("applicant_id")

            if existing_session and not existing_session.get("closed") and not pdf_path:
                # Continuation reply to clarification questions without a new PDF attachment
                clean_reply = strip_email_reply_history(body)
                quote_fields = {f.name for f in dataclasses.fields(ExtractedQuote)}
                cached_data = {k: v for k, v in existing_session.get("quote", {}).items() if k in quote_fields}
                cached_quote = ExtractedQuote(**cached_data)

                attempts = existing_session.get("clarification_attempts", 1) + 1
                existing_session["clarification_attempts"] = attempts

                logger.info("Resuming Ascend thread session %s (turn %d)", thread_id, attempts)
                result = manager.resume_with_clarifications(
                    quote=cached_quote,
                    clarification_reply=clean_reply or body,
                    sender_email=sender,
                    sender_name=sender.split("@")[0].title() if sender else "",
                    applicant_id=applicant_id,
                    clarification_attempts=attempts,
                )

                existing_session["status"] = result.status
                existing_session["quote"] = dataclasses.asdict(result.quote)
                existing_session["updated_at"] = time.time()
                if result.status in ("COMPLETED", "ESCALATED"):
                    existing_session["closed"] = True
                sessions[thread_id] = existing_session
                save_ascend_sessions(sessions)

                if result.reply_email_body:
                    response_text = result.reply_email_body
                logger.info("Resumed Ascend workflow for thread %s (status=%s)", thread_id, result.status)

            else:
                # Brand-new quote intake or email with attached quote PDF
                quote_source = Path(pdf_path) if pdf_path else body
                result = manager.process_quote_request(
                    raw_text_or_pdf=quote_source,
                    user_instruction=body,
                    sender_email=sender,
                    sender_name=sender.split("@")[0].title() if sender else "",
                    applicant_id=applicant_id,
                )
                sessions[thread_id] = {
                    "thread_id": thread_id,
                    "sender": sender,
                    "applicant_id": applicant_id,
                    "pdf_path": str(pdf_path) if pdf_path else None,
                    "clarification_attempts": 1 if result.status == "NEEDS_CLARIFICATION" else 0,
                    "status": result.status,
                    "quote": dataclasses.asdict(result.quote),
                    "closed": result.status not in ("NEEDS_CLARIFICATION",),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
                save_ascend_sessions(sessions)

                if result.status in ("COMPLETED", "NEEDS_CLARIFICATION", "ESCALATED") and result.reply_email_body:
                    response_text = result.reply_email_body
                    logger.info("Handled Ascend workflow deterministically (status=%s)", result.status)
        except Exception as exc:
            logger.warning("Deterministic Ascend workflow outcome requires review: %s", type(exc).__name__)
            return "ROBIE_OUTCOME_UNKNOWN: Finance execution stopped after an error. Check the destination before trying again; no automatic second execution was started."

    if is_ascend_request:
        store.checkpoint(job_id, 'email_route', {'route': 'finance', 'status': result.status})
        if not response_text:
            return "ROBIE_OUTCOME_UNKNOWN: Finance workflow returned no final response; review the recorded destination before retrying."
    # Hard route (code, not prompt): homeowners-create on 220250093.
    # The runner classifies in code and checkpoints the marker. The tool layer
    # (playwright_tool) then calls ezlynx_policy_setup in code before any
    # playwright_exec browser code runs. The runner never sets up the policy
    # itself and no prompt text is involved.
    if not is_ascend_request:
        from robie_job_engine.policy_setup_dispatch import (
            POLICY_SETUP_REQUIRED_KIND,
            extract_policy_setup_args,
        )
        policy_setup_args = extract_policy_setup_args(f"{subject}\n{body}")
        if policy_setup_args:
            store.checkpoint(job_id, 'email_route', {
                'route': 'policy_setup_hard_route',
                'policy_number': policy_setup_args["policy_number"],
                'effective_date': policy_setup_args["effective_date"],
                'expiration_date': policy_setup_args["expiration_date"],
            })
            store.checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND, {
                **policy_setup_args,
                'tool_called': False,
            })
            logger.info(
                "Hard-routed policy setup for %s",
                policy_setup_args["policy_number"],
            )
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
            f"CRITICAL MANDATORY INBOUND EMAIL RULE:\n"
            f"- ALWAYS save this communication and its details back to EZLynx via API directly to the client file.\n"
            f"- Even if this email is internal (e.g. from StreetSmart team members/staff) and carrier-related (carrier quotes, policy changes, underwriter replies, endorsements, cancellations, certificates, or audit queries), you MUST locate the matching client account/policy in EZLynx and post a discussion note and upload any attached documents directly to the client file via the EZLynx REST API (`EZLynxApiClient` or `EZLynxAgreementPoster.post_custom_note`).\n\n"
            f"Instructions:\n"
            f"1. If the user is asking to create an Ascend payment agreement or finance agreement (or sending an insurance quote for agreement generation):\n"
            f"   - Use the 'ascend-api-create-program' skill or the Ascend API Python tools (`robie_job_engine.ascend_workflow.AscendWorkflowManager` or `QuoteExtractor`).\n"
            f"   - Check if agency fee, commission rate, surplus lines tax, and terrorism coverage are clear from the email body or attached PDF quote.\n"
            f"   - If any of those 4 parameters are missing or ambiguous (e.g. quote has options with/without terrorism), ask {sender} to clarify what they want.\n"
            f"   - Once clear or if already specified, generate the program via Ascend API, post the checkout link discussion note to EZLynx, and provide {sender} the Ascend checkout link and quote breakdown.\n"
            f"1b. If the user is asking to create, set up, or complete a homeowners policy on EZLynx (e.g. TEST-HO-20260911-E01 on applicant 220250093):\n"
            f"   - Call the 'ezlynx_policy_setup' tool FIRST — not playwright_exec, not a hand-rolled browser script. It runs the Job Engine path: search-first, gold carrier create, Save & Continue Edit, FormEntry coverages by label.\n"
            f"   - Pass policy_number, effective_date, expiration_date, and any coverage limits stated in the email.\n"
            f"2. For any other request, execute the required insurance operations skill and assist thoroughly, ensuring the communication is filed back to the EZLynx client file.\n"
            f"3. Write a professional, concise, polished email response directly addressing {sender}."
        )

        response_text = run_agent_task(task_prompt + "\n\n" + context_prompt, job_id, db_path)
    return response_text


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

        try:
            response_text = run_guarded_email_task(
                db_path=JOB_DB, gmail_message_id=msg_id,
                prompt=f"Subject: {subject}\n\n{body}", run_agent=run_agent_task,
                attachment_names=tuple(name for name, _ in attachments),
                run_agent_with_context=lambda prompt, job_id, db_path: run_email_job(
                    prompt, job_id, db_path, sender=sender, subject=subject, body=body,
                    attachments=attachments, thread_id=thread_id),
            )
        except EmailTaskPending:
            logger.info("Durable email job is pending; leaving message unread")
            continue

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
    if sys.argv[1:] == ['--execute-job']:
        from contextlib import redirect_stdout
        request = json.load(sys.stdin)
        with redirect_stdout(sys.stderr):
            response = execute_email_work(**request)
        print(json.dumps({'response': response}))
    elif sys.argv[1:]:
        raise SystemExit('Unknown email agent arguments')
    else:
        process_inbox()
