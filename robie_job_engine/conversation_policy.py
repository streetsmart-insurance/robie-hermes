"""Deterministic routing and read-only status for trusted conversational turns.

Routing is deliberately conservative, not a semantic classifier or factuality
proof. Informational model text is visibly labelled and has no tool authority.
"""
from __future__ import annotations

import re
import hashlib
import json
import sqlite3
from pathlib import Path

INFORMATIONAL = 'informational'
STATUS = 'status'
OPERATIONAL = 'operational'
CLARIFY = 'clarify'
INFORMATIONAL_FRAME = (
    'Informational answer — no actions were taken for this reply. '
    'This explanation may contain mistakes and is not verified job status.\n\n'
)
CLARIFY_REPLY = (
    'Are you asking for an explanation, the status of an existing job, or a new action? '
    'Please separate any action request from the question. No actions were taken for this reply.'
)
NO_STATUS = (
    'I cannot identify one verified job owned by you in this thread. '
    'Please ask in the original job thread and include its job reference. '
    'I am not reporting the work as complete.'
)
_ACTION = r'(?:send|email|upload|move|delete|apply|create|write|update|edit|submit|renew|cancel|bind|issue|pay|file|process|execute|approve)'
_REQUEST = re.compile(r'^(?:please\s+)?(?:' + _ACTION + r'\b|(?:can|could|would|will) you\s+' + _ACTION + r'\b)', re.I)
_ADDITIONAL = re.compile(r'(?:[.;!?\n]|\b(?:and|then|also)\b)\s*(?:please\s+)?(?:' + _ACTION + r'\b|(?:can|could|would|will) you\s+' + _ACTION + r'\b)', re.I)
_STATUS = re.compile(r'\b(?:status|progress|job reference)\b|^(?:what happened|what did you do|what are you working on|where did we leave off|how is it going|how are we doing|(?:can you )?give me a rundown|can you tell me what was done|did you|have you|has (?:it|the)|is (?:it|the job|this|that) (?:done|complete|finished))\b', re.I)
# Outcome questions about a particular object are status requests, even without
# the literal word status. This is input routing, not a filter on model prose.
_OUTCOME_QUESTION = re.compile(
    r'^(?:is|are|was|were|has|have|(?:can|could|would) you(?: tell me)? (?:if|whether))\b.*\b(?:renewed|uploaded|complete[ds]?|finished|done|sent|emailed|filed|saved|updated|submitted|processed|cancelled|canceled|bound|issued|paid|approved|created|deleted|verified)\b', re.I)
_RESULT_QUESTION = re.compile(r'^(?:what|when|how)\b(?=.*\b(?:result|outcome|completion)\b)(?=.*\b(?:my|our|this|that|the)\b)', re.I)
_INFO = re.compile(r'^(?:explain|describe|tell me about|what (?:is|are|does|do)|why|how (?:to|do|does|can|should)|when|where|who|is|are)\b', re.I)
_JOB_ID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)


def classify(text: str, *, attachments: int = 0) -> str:
    if attachments or str(text or '').lstrip().startswith('/'):
        return OPERATIONAL
    text = str(text or '').strip()
    from .engine import is_retry_text
    if is_retry_text(text):
        return OPERATIONAL
    if _ADDITIONAL.search(text):
        return CLARIFY
    if _REQUEST.search(text):
        return OPERATIONAL
    if not _JOB_ID.search(text) and (
        re.match(r'^(?:explain|describe) (?:the )?(?:status meanings|job statuses|meaning of)', text, re.I)
        or re.match(r'^how (?:to|do i|can i|should i)\b', text, re.I)):
        return INFORMATIONAL
    status_text = re.sub(r'^(?:explain|describe)\s+', '', text, flags=re.I)
    if (_STATUS.search(status_text) or _OUTCOME_QUESTION.search(status_text)
        or _RESULT_QUESTION.search(status_text) or _JOB_ID.search(text)):
        return STATUS
    if text.casefold().strip(' .!?') in {'hello', 'hi', 'hey', 'good morning', 'good afternoon', 'good evening'}:
        return INFORMATIONAL
    if _INFO.search(text):
        return INFORMATIONAL
    # Preserve the existing harmless greetings and explicit synthetic markers.
    from .chat_guard import chat_message_requires_job
    if not chat_message_requires_job(text):
        return INFORMATIONAL
    return CLARIFY


def pending_reply_is_conversation(text: str) -> bool:
    """Questions/status/mixed actions cannot become a missing-field value.

    Short data, yes/no, and retry are left to the existing typed HITL classifier.
    This function grants no resume authority; owner/context checks happen first.
    """
    text = str(text or '').strip()
    lane = classify(text)
    return lane == STATUS or bool(_ADDITIONAL.search(text)) or bool(
        lane == INFORMATIONAL and _INFO.search(text))


def informational_answer(response: str) -> str:
    # Always server-prefix it; a model cannot select status authority or remove
    # the warning. This is a factual-risk disclosure, not a truth guarantee.
    return INFORMATIONAL_FRAME + response.strip()


def status_answer(db_path, event, *, environment: str | None) -> str:
    """Read-only SQLite snapshot, no migrations/transitions or active-space fallback.

    Require a unique exact thread and trusted ingress-owner checkpoint. COMPLETE
    alone is insufficient; authoritative verified destination receipts must match
    their stored digest. These are stored records, not a new live business check.
    """
    source = event.source
    actor, space, thread = str(source.user_id), str(source.chat_id), str(source.thread_id or '')
    if not actor or not thread or not environment:
        return NO_STATUS
    explicit = _JOB_ID.findall(str(event.text or ''))
    try:
        if len(set(explicit)) > 1:
            return NO_STATUS
        # mode=ro prevents both accidental database creation and ledger writes.
        with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            if explicit:
                candidates = conn.execute('SELECT * FROM jobs WHERE id=?', (explicit[0],)).fetchall()
            else:
                candidates = conn.execute(
                    "SELECT DISTINCT j.* FROM jobs j LEFT JOIN checkpoints c ON c.job_id=j.id AND c.kind='chat_thread' "
                    "WHERE j.payload_json LIKE ? OR c.data_json LIKE ? LIMIT 101",
                    (f'%{thread}%', f'%{thread}%')).fetchall()
                if len(candidates) > 100:
                    return NO_STATUS
            owned = []
            for job in candidates:
                def checkpoint(kind):
                    row = conn.execute('SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?',
                        (job['id'], kind)).fetchone()
                    return json.loads(row['data_json']) if row else {}
                owner = checkpoint('chat_request_owner')
                payload = json.loads(job['payload_json'])
                stored_thread = checkpoint('chat_thread').get('thread_name') or payload.get('thread_id') or payload.get('thread_name')
                if (owner.get('actor') == actor and owner.get('environment') == environment
                    and payload.get('conversation_id') == space and stored_thread == thread):
                    owned.append(job)
            if len(owned) != 1:
                return NO_STATUS
            job = owned[0]
            from .models import JobStatus
            from .store import canonical_json
            status = JobStatus(job['status'])
            # The same pure completion checks used by JobEngine/JobStore; no
            # transition or verifier authority is manufactured by this reader.
            from .complete_guard import (
                complete_is_prohibited, postcondition_mismatch, evidence_is_stale,
                destination_identity_missing, intended_destination_identity,
            )
            from .ezlynx_api_only_writes import note_or_document_write_missing_api_id
            action_row = conn.execute("SELECT created_at,data_json FROM checkpoints WHERE job_id=? AND kind='action'", (job['id'],)).fetchone()
            action = json.loads(action_row['data_json']) if action_row else None
            payload = json.loads(job['payload_json'])
            perform = conn.execute("SELECT created_at FROM attempts WHERE job_id=? AND phase='perform' ORDER BY id DESC LIMIT 1", (job['id'],)).fetchone()
            floors = [perform['created_at'] if perform else None, action_row['created_at'] if action_row else None]
            stored_floor = max((item for item in floors if item), default=job['created_at'])
            evidence = conn.execute('SELECT * FROM verification_evidence WHERE job_id=?', (job['id'],)).fetchall()
            def valid_receipt(row):
                expected, observed = json.loads(row['expected_json']), json.loads(row['observed_json'])
                body = canonical_json({'method': row['method'], 'source': row['source'],
                    'expected': expected, 'observed': observed, 'authoritative': bool(row['authoritative']),
                    'captured_at': row['captured_at'], 'locator': row['locator']})
                return (row['verified'] == 1 and row['authoritative'] == 1 and row['source']
                    and row['method'] and row['locator'] and row['captured_at'] and expected and observed
                    and hashlib.sha256(body.encode()).hexdigest() == row['evidence_sha256']
                    and not complete_is_prohibited(observed)
                    and not postcondition_mismatch(expected, observed)
                    and not evidence_is_stale(captured_at=row['captured_at'], not_before=job['created_at'],
                        stored_at=row['created_at'], stored_not_before=stored_floor)
                    and not destination_identity_missing(locator=row['locator'], expected=expected,
                        observed=observed, intended=intended_destination_identity(action=action, payload=payload), job_id=job['id'])
                    and not note_or_document_write_missing_api_id(expected=expected, observed=observed,
                        action=action, payload=payload))
            verified = bool(evidence) and all(valid_receipt(row) for row in evidence)
            if status == JobStatus.COMPLETE:
                line = (f'Recorded complete with {len(evidence)} verified authoritative destination receipt(s).'
                    if verified else 'Not verified — the completion flag has no sufficient destination receipts. Treat as incomplete.')
            else:
                line = f'Recorded status: {status.value}. I am not reporting this job as complete.'
            return f'Job-record lookup (stored evidence; no new live check)\nJob {job["id"]}\n{line}'
    except Exception:
        return NO_STATUS


def pending_input_owned(db_path, event, context, *, environment: str | None) -> bool:
    """Gate the existing durable resume before it can bind/edit another Job.

    Main-DM replies may use that DM's unique active correlation; an explicit
    side/group thread must match the stored Job thread. Missing owner fails closed.
    """
    source = event.source
    raw = getattr(event, 'raw_message', None) or {}
    sender = raw.get('sender') or {}
    actor = str(source.user_id or '')
    from .chat_turn_control import sender_is_allowed
    if (not actor or sender.get('type') != 'HUMAN'
        or actor != str(sender.get('email') or sender.get('name') or '')
        or not sender_is_allowed(actor) or not environment or not context):
        return False
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            ident = context['job_id']
            job = conn.execute('SELECT payload_json FROM jobs WHERE id=?', (ident,)).fetchone()
            row = conn.execute("SELECT data_json FROM checkpoints WHERE job_id=? AND kind='chat_request_owner'", (ident,)).fetchone()
            owner = json.loads(row['data_json']) if row else {}
            payload = json.loads(job['payload_json']) if job else {}
            row = conn.execute("SELECT data_json FROM checkpoints WHERE job_id=? AND kind='chat_thread'", (ident,)).fetchone()
            stored = (json.loads(row['data_json']).get('thread_name') if row else None) or payload.get('thread_id') or payload.get('thread_name')
            thread = str(source.thread_id or '')
            return bool(owner.get('actor') == actor and owner.get('environment') == environment
                and payload.get('conversation_id') == source.chat_id
                and context.get('conversation_id') == source.chat_id
                and (thread == stored if thread else getattr(source, 'chat_type', None) == 'dm'))
    except Exception:
        return False
