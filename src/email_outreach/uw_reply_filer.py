"""Underwriter reply → titled EZLynx discussion filing.

Polls **robie@** and **hello@** only (never CSR / org-wide inboxes), matches
`[RENEWAL-REQ-###]` and/or policy number, and posts onto an existing titled
renewal discussion via `find_matching_discussion`. Does not create orphan or
untitled cards. Filing is additive — the inbox cleaner must not trash these
replies.

Hooked from:
- Daily cadence (`OutreachCadenceManager.process_incoming_inbox_replies`)
- Robie inbox cleaner (additive; cleanup continues if filing fails)
- Standalone: `PYTHONPATH=. python3 -m src.email_outreach.uw_reply_filer`
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.config import settings
from src.database.models import (
    ActionType,
    AuditNoteLog,
    DocumentRecord,
    OutreachThread,
    PolicyRenewal,
    RenewalStatus,
    ThreadStatus,
)
from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.intent_classifier import (
    EmailClassification,
    UnderwriterIntentClassifier,
)
from src.ezlynx.api_client import EZLynxApiClient
from src.ezlynx.document_uploader import FOLDER_ROUTING, LABEL_ROUTING
from src.ezlynx.note_builder import EZLynxNoteBuilder

logger = logging.getLogger("uw_reply_filer")

RENEWAL_REQ_RE = re.compile(r"\[RENEWAL-REQ-(\d+)\]", re.IGNORECASE)

EXCLUDED_STATUSES = {
    RenewalStatus.EXCLUDED_INACTIVE_ACCOUNT,
    RenewalStatus.EXCLUDED_TEST_ACCOUNT,
}


def extract_renewal_req_id(*texts: Optional[str]) -> Optional[int]:
    """Returns the numeric id inside the first `[RENEWAL-REQ-###]` tag found."""
    for text in texts:
        if not text:
            continue
        match = RENEWAL_REQ_RE.search(text)
        if match:
            return int(match.group(1))
    return None


def is_allowed_filing_inbox(inbox_source: Optional[str]) -> bool:
    """True only for robie@ / hello@ (settings.gmail_poll_inboxes)."""
    if not inbox_source:
        return False
    allowed = {str(e).strip().lower() for e in (settings.gmail_poll_inboxes or [])}
    if not allowed:
        allowed = {"robie@streetsmart.insurance", "hello@streetsmart.insurance"}
    return inbox_source.strip().lower() in allowed


def is_fileable_policy(policy: Optional[PolicyRenewal]) -> bool:
    if policy is None or not policy.applicant_id:
        return False
    status = policy.status
    if status in EXCLUDED_STATUSES:
        return False
    return True


def _fileable_policy_dicts(policies: Sequence[PolicyRenewal]) -> List[Dict[str, Any]]:
    return [
        {"id": p.id, "policy_number": p.policy_number, "insured_name": p.insured_name}
        for p in policies
        if is_fileable_policy(p)
    ]


def reply_already_filed(db: Session, policy_id: int, message_id: Optional[str]) -> bool:
    """Idempotency: skip if an UNDERWRITER_REPLIED audit note already cites this Gmail id."""
    if not message_id:
        return False
    existing = (
        db.query(AuditNoteLog)
        .filter(
            AuditNoteLog.policy_id == policy_id,
            AuditNoteLog.action_type == ActionType.UNDERWRITER_REPLIED,
            AuditNoteLog.note_text.contains(message_id),
        )
        .first()
    )
    return existing is not None


def match_policy_for_reply(
    db: Session,
    reply: Dict[str, Any],
    policies: Sequence[PolicyRenewal],
    gmail: Optional[GmailRenewalClient] = None,
) -> Optional[PolicyRenewal]:
    """Match an inbound message to a PolicyRenewal using existing outreach helpers.

    Order (no new threading rules):
    1. `[RENEWAL-REQ-###]` → policy id / tracking_code
    2. `matched_policy_id` already stamped by `GmailRenewalClient._matches_any_policy`
    3. Policy number / insured name via `_matches_any_policy`
    """
    subject = reply.get("subject", "") or ""
    body = reply.get("body", "") or ""
    req_id = extract_renewal_req_id(subject, body)

    if req_id:
        pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == req_id).first()
        if is_fileable_policy(pol):
            return pol
        thread = (
            db.query(OutreachThread)
            .filter(OutreachThread.tracking_code == f"RENEWAL-REQ-{req_id}")
            .first()
        )
        if thread and is_fileable_policy(thread.policy):
            return thread.policy

    matched_id = reply.get("matched_policy_id")
    if matched_id:
        pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == matched_id).first()
        if is_fileable_policy(pol):
            return pol

    matcher = gmail or GmailRenewalClient(service=None, inbox_services={})
    parsed = dict(reply)
    if matcher._matches_any_policy(parsed, _fileable_policy_dicts(policies)):
        pol_id = parsed.get("matched_policy_id")
        if pol_id:
            pol = db.query(PolicyRenewal).filter(PolicyRenewal.id == pol_id).first()
            if is_fileable_policy(pol):
                return pol
    return None


INTENT_DOC_TYPE = {
    "QUOTE_ATTACHED": "renewal",
    "LOSS_RUNS_ATTACHED": "loss runs",
    "NON_RENEWAL_DECLINED": "non renewal",
}


def doc_type_for_intent(classification: EmailClassification) -> str:
    """Map classifier intent onto document_uploader FOLDER_ROUTING / LABEL_ROUTING keys."""
    return INTENT_DOC_TYPE.get(
        classification.intent,
        getattr(classification, "document_type", None) or "renewal",
    )


def folder_and_label_for_doc_type(doc_type: str) -> tuple[Optional[str], Optional[str]]:
    """Reuse EZLynxDocumentUploader folder/label tables — do not invent new destinations."""
    key = (doc_type or "renewal").strip().lower()
    folders = FOLDER_ROUTING.get(key) or FOLDER_ROUTING.get("renewal") or []
    folder = folders[0] if folders else None
    label = LABEL_ROUTING.get(key) or LABEL_ROUTING.get("renewal")
    return folder, label


def _upload_attachments_and_maybe_task(
    ezlynx: EZLynxApiClient,
    policy: PolicyRenewal,
    classification: EmailClassification,
    attached_file_path: Optional[str],
) -> bool:
    """Upload via EZLynxApiClient → document_uploader routing; create the existing CSR task."""
    if not attached_file_path:
        return False

    doc_type = doc_type_for_intent(classification)
    folder_name, label_to_apply = folder_and_label_for_doc_type(doc_type)
    ezlynx.upload_document(
        applicant_id=policy.applicant_id,
        file_path=Path(attached_file_path),
        folder_name=folder_name,
        policy_number=policy.policy_number,
        doc_type=doc_type,
        label_to_apply=label_to_apply,
    )

    if classification.intent == "QUOTE_ATTACHED":
        task_res = ezlynx.create_user_task(
            applicant_id=policy.applicant_id,
            title=f"Review Renewal Quote: {policy.insured_name} ({policy.carrier_name})",
            description=(
                f"Underwriter emailed renewal proposal for Pol #{policy.policy_number}. "
                f"Document uploaded to Documents > {folder_name}."
            ),
            assigned_user=policy.assigned_agent,
        )
        return bool(task_res and task_res.get("status") not in ("simulated", "error", None))

    if classification.intent == "LOSS_RUNS_ATTACHED":
        task_res = ezlynx.create_user_task(
            applicant_id=policy.applicant_id,
            title=f"Loss Runs Received: {policy.insured_name} ({policy.carrier_name})",
            description=(
                f"Underwriter/carrier provided loss runs for Pol #{policy.policy_number}. "
                f"Document uploaded to Documents > {folder_name}."
            ),
            assigned_user=policy.assigned_agent,
        )
        return bool(task_res and task_res.get("status") not in ("simulated", "error", None))

    if classification.intent == "NON_RENEWAL_DECLINED":
        task_res = ezlynx.create_user_task(
            applicant_id=policy.applicant_id,
            title=f"CRITICAL: Non-Renewal Notice - Re-market {policy.insured_name} ({policy.carrier_name})",
            description=(
                f"Carrier issued non-renewal notice for Pol #{policy.policy_number}. "
                f"Document uploaded to Documents > {folder_name}."
            ),
            assigned_user=policy.assigned_agent,
        )
        return bool(task_res and task_res.get("status") not in ("simulated", "error", None))

    return False


def file_single_reply(
    db: Session,
    reply: Dict[str, Any],
    policy: PolicyRenewal,
    ezlynx: EZLynxApiClient,
    classifier: UnderwriterIntentClassifier,
    gmail: Optional[GmailRenewalClient] = None,
    dry_run: bool = False,
    alert_csr: bool = True,
) -> Dict[str, Any]:
    """Classify one reply, locate the existing titled card, post, and alert CSR + Carlo."""
    message_id = reply.get("message_id")
    inbox_source = reply.get("inbox_source", "")
    subject = reply.get("subject", "")

    if reply_already_filed(db, policy.id, message_id):
        logger.info(
            f"Skipping already-filed UW reply {message_id} for Pol #{policy.policy_number}"
        )
        return {"status": "skipped", "reason": "already_filed", "message_id": message_id}

    matched = ezlynx.find_matching_discussion(
        applicant_id=str(policy.applicant_id),
        policy_number=policy.policy_number,
        line_of_business=policy.line_of_business,
        carrier_name=policy.carrier_name,
    )
    if not matched or not matched.get("title"):
        logger.warning(
            f"No existing titled renewal discussion for applicant {policy.applicant_id} "
            f"/ Pol #{policy.policy_number}; refusing orphan card creation."
        )
        return {
            "status": "skipped",
            "reason": "no_existing_titled_discussion",
            "message_id": message_id,
            "policy_number": policy.policy_number,
        }

    discussion_title = matched["title"]
    thread = db.query(OutreachThread).filter(OutreachThread.policy_id == policy.id).first()
    tracking_code = thread.tracking_code if thread else f"RENEWAL-REQ-{policy.id}"

    att_paths = [a["path"] for a in reply.get("attachments", []) if a.get("path")]
    att_names = [a.get("filename") for a in reply.get("attachments", []) if a.get("filename")]

    classification: EmailClassification = classifier.classify(
        subject=subject,
        body_text=reply.get("body", ""),
        attachment_filenames=att_names,
    )

    note_text = EZLynxNoteBuilder.format_reply_received_note(
        policy=policy,
        tracking_code=tracking_code,
        sender_email=reply.get("sender", ""),
        intent=classification.intent,
        summary=classification.summary,
        has_attachment=bool(att_paths),
        attachment_name=att_names[0] if att_names else None,
        clean_reply_text=reply.get("clean_reply_text"),
        inbox_source=inbox_source or None,
        gmail_message_id=message_id,
    )

    if dry_run:
        logger.info(
            f"[DRY-RUN] Would file UW reply {message_id} onto '{discussion_title}' "
            f"for Pol #{policy.policy_number}"
        )
        return {
            "status": "dry_run",
            "discussion_title": discussion_title,
            "note_text": note_text,
            "message_id": message_id,
            "policy_number": policy.policy_number,
        }

    if thread:
        thread.latest_reply_summary = f"[{inbox_source} | {classification.intent}] {classification.summary}"
        thread.status = ThreadStatus.REPLIED

    attached_file_path = None
    if att_paths:
        attached_file_path = str(att_paths[0])
        db.add(
            DocumentRecord(
                policy_id=policy.id,
                file_name=att_names[0] if att_names else Path(attached_file_path).name,
                file_path=attached_file_path,
                source=f"GMAIL_{(inbox_source or 'inbox').split('@')[0].upper()}",
            )
        )

    if classification.intent == "QUOTE_ATTACHED":
        policy.status = RenewalStatus.QUOTE_RECEIVED
        if thread:
            thread.status = ThreadStatus.RESOLVED
    elif classification.intent == "INFO_REQUESTED":
        policy.status = RenewalStatus.INFO_REQUESTED
    elif classification.intent == "NON_RENEWAL_DECLINED":
        policy.status = RenewalStatus.NON_RENEWAL_DECLINED
        if thread:
            thread.status = ThreadStatus.RESOLVED

    db.add(
        AuditNoteLog(
            policy_id=policy.id,
            applicant_id=policy.applicant_id,
            discussion_title=discussion_title,
            action_type=ActionType.UNDERWRITER_REPLIED,
            note_text=note_text,
        )
    )

    note_res = ezlynx.add_note_to_discussion(
        applicant_id=policy.applicant_id,
        discussion_title=discussion_title,
        note_text=note_text,
        policy_number=policy.policy_number,
        line_of_business=policy.line_of_business,
        carrier_name=policy.carrier_name,
        require_existing_discussion=True,
    )
    if isinstance(note_res, dict) and isinstance(note_res.get("discussion_title"), str):
        policy.discussion_title = note_res.get("discussion_title")
    note_synced = bool(note_res and note_res.get("status") not in ("simulated", "error", None))
    note_id = note_res.get("note_id") if note_res else None

    if note_res and note_res.get("status") == "error":
        logger.warning(
            f"EZLynx note post blocked/failed for Pol #{policy.policy_number}: "
            f"{note_res.get('error')}"
        )
        return {
            "status": "skipped",
            "reason": note_res.get("error") or "note_post_failed",
            "message_id": message_id,
            "policy_number": policy.policy_number,
        }

    task_created = _upload_attachments_and_maybe_task(
        ezlynx, policy, classification, attached_file_path
    )

    if alert_csr:
        try:
            from src.reporting.email_handoff import notify_csr_of_underwriter_reply

            notify_csr_of_underwriter_reply(
                policy=policy,
                classification=classification,
                sender=reply.get("sender", ""),
                attachments=reply.get("attachments", []),
                client=gmail,
                note_synced=note_synced,
                task_created=task_created,
                note_id=note_id,
                clean_reply_text=reply.get("clean_reply_text"),
            )
        except Exception as exc:
            logger.error(f"Failed to send CSR/Carlo alert for Pol #{policy.policy_number}: {exc}")

    if message_id and gmail:
        gmail.mark_message_read(message_id, inbox_source)

    return {
        "status": "filed",
        "discussion_title": policy.discussion_title or discussion_title,
        "note_id": note_id,
        "note_synced": note_synced,
        "message_id": message_id,
        "policy_number": policy.policy_number,
        "note_text": note_text,
    }


def file_inbox_replies(
    db: Session,
    gmail: Optional[GmailRenewalClient] = None,
    ezlynx: Optional[EZLynxApiClient] = None,
    classifier: Optional[UnderwriterIntentClassifier] = None,
    replies: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
    alert_csr: bool = True,
) -> Dict[str, Any]:
    """Poll robie@ + hello@ (or accept injected replies) and file onto titled cards."""
    gmail = gmail or GmailRenewalClient()
    ezlynx = ezlynx or EZLynxApiClient()
    classifier = classifier or UnderwriterIntentClassifier()

    policies = [
        p
        for p in db.query(PolicyRenewal).all()
        if is_fileable_policy(p)
    ]

    if replies is None:
        # poll_matching_replies itself restricts to robie@ + hello@ (gmail_poll_inboxes).
        replies = gmail.poll_matching_replies(active_policies=_fileable_policy_dicts(policies))

    if not isinstance(replies, list):
        logger.warning("poll_matching_replies did not return a list; skipping UW filing.")
        return {"filed": 0, "skipped": 0, "errors": 0, "results": [], "error": "invalid_poll_result"}

    results: List[Dict[str, Any]] = []
    filed = skipped = errors = 0

    for reply in replies:
        if not isinstance(reply, dict):
            skipped += 1
            continue

        inbox_source = reply.get("inbox_source", "")
        if inbox_source and not is_allowed_filing_inbox(inbox_source):
            logger.info(f"Skipping reply from out-of-scope inbox: {inbox_source}")
            skipped += 1
            results.append(
                {
                    "status": "skipped",
                    "reason": "inbox_out_of_scope",
                    "inbox_source": inbox_source,
                    "message_id": reply.get("message_id"),
                }
            )
            continue

        if not inbox_source:
            # Injected test replies may omit inbox_source; treat as in-scope.
            reply = dict(reply)
            reply.setdefault("inbox_source", "robie@streetsmart.insurance")

        try:
            policy = match_policy_for_reply(db, reply, policies, gmail=gmail)
            if not policy:
                skipped += 1
                results.append(
                    {
                        "status": "skipped",
                        "reason": "unmatched_policy",
                        "subject": reply.get("subject"),
                        "message_id": reply.get("message_id"),
                    }
                )
                continue

            outcome = file_single_reply(
                db=db,
                reply=reply,
                policy=policy,
                ezlynx=ezlynx,
                classifier=classifier,
                gmail=gmail,
                dry_run=dry_run,
                alert_csr=alert_csr,
            )
            results.append(outcome)
            if outcome.get("status") == "filed" or outcome.get("status") == "dry_run":
                filed += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            logger.exception(f"Error filing UW reply {reply.get('message_id')}: {exc}")
            results.append(
                {
                    "status": "error",
                    "error": str(exc),
                    "message_id": reply.get("message_id"),
                }
            )

    if not dry_run:
        db.commit()

    summary = {
        "filed": filed,
        "skipped": skipped,
        "errors": errors,
        "results": results,
        "dry_run": dry_run,
    }
    logger.info(
        f"UW reply filing complete: filed={filed} skipped={skipped} errors={errors} dry_run={dry_run}"
    )
    return summary


def run_uw_reply_filing(
    gmail_client: Optional[GmailRenewalClient] = None,
    ezlynx_api: Optional[EZLynxApiClient] = None,
    dry_run: bool = False,
    alert_csr: bool = True,
) -> Dict[str, Any]:
    """Opens a DB session and files UW replies. Safe to call from cleaner / cron."""
    from src.database.session import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        return file_inbox_replies(
            db=db,
            gmail=gmail_client,
            ezlynx=ezlynx_api,
            dry_run=dry_run,
            alert_csr=alert_csr,
        )
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "File underwriter replies from robie@ and hello@ onto existing titled "
            "EZLynx renewal discussion cards. Does not scan CSR / org-wide inboxes."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and match replies without posting notes, alerting, or marking read.",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="Skip CSR + Carlo email alert (still posts the EZLynx note unless --dry-run).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    summary = run_uw_reply_filing(dry_run=args.dry_run, alert_csr=not args.no_alert)
    print(
        f"UW reply filing finished: filed={summary.get('filed')} "
        f"skipped={summary.get('skipped')} errors={summary.get('errors')} "
        f"dry_run={summary.get('dry_run')}"
    )


if __name__ == "__main__":
    main()
