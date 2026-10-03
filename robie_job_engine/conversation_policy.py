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
    if _STATUS.search(status_text) or _JOB_ID.search(text):
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
            evidence = conn.execute('SELECT * FROM verification_evidence WHERE job_id=?', (job['id'],)).fetchall()
            def valid_receipt(row):
                expected, observed = json.loads(row['expected_json']), json.loads(row['observed_json'])
                body = canonical_json({'method': row['method'], 'source': row['source'],
                    'expected': expected, 'observed': observed, 'authoritative': bool(row['authoritative']),
                    'captured_at': row['captured_at'], 'locator': row['locator']})
                return (row['verified'] == 1 and row['authoritative'] == 1 and row['source']
                    and row['method'] and row['locator'] and row['captured_at'] and expected and observed
                    and hashlib.sha256(body.encode()).hexdigest() == row['evidence_sha256'])
            verified = bool(evidence) and all(valid_receipt(row) for row in evidence)
            if status == JobStatus.COMPLETE:
                line = (f'Recorded complete with {len(evidence)} verified authoritative destination receipt(s).'
                    if verified else 'Not verified — the completion flag has no sufficient destination receipts. Treat as incomplete.')
            else:
                line = f'Recorded status: {status.value}. I am not reporting this job as complete.'
            return f'Job-record lookup (stored evidence; no new live check)\nJob {job["id"]}\n{line}'
    except Exception:
        return NO_STATUS
