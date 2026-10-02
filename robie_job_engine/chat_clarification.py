"""Deliver a reply only to the authorized, still-running clarification owner."""
from __future__ import annotations

import json
from typing import Callable


def register_question(store, job_id: str, generation: str, clarify_id: str) -> bool:
    with store.transaction() as conn:
        def checkpoint(kind):
            row = conn.execute('SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?', (job_id, kind)).fetchone()
            return json.loads(row[0]) if row else {}
        state = checkpoint('model_generation')
        owner = checkpoint('chat_request_owner')
        if state.get('generation') != generation or not state.get('running') or not owner.get('actor'):
            return False
        from .turn_finalization import _save_generation
        _save_generation(conn, job_id, 'chat_live_question', {
            'generation': generation, 'clarify_id': clarify_id, 'open': True,
            'actor': owner['actor'], 'environment': owner['environment'],
        })
    return True


def deliver_reply(store, *, thread: str, actor: str, environment: str,
                  message_id: str, text: str, resolve: Callable[[str, str], bool],
                  clarify_id: str = '') -> str:
    """Return absent, new_intent, refused or delivered. A refused reply cannot start a turn.

    The DB transaction serializes duplicate answers against the durable generation.
    Hermes resolves only the exact clarify id; it never wakes a whole DM session.
    """
    from .turn_finalization import _save_generation
    if not thread:
        return 'absent'
    with store.transaction() as conn:
        # Do not use the best-effort display lookup: store/read failures must
        # propagate to the Pub/Sub coordinator (NACK), never open a new job.
        rows = conn.execute(
            "SELECT job_id,data_json FROM checkpoints WHERE kind='chat_thread'"
        ).fetchall()
        owners = [row['job_id'] for row in rows
                  if json.loads(row['data_json']).get('thread_name') == thread]
        def checkpoint(ident, kind):
            row = conn.execute('SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?', (ident, kind)).fetchone()
            return json.loads(row[0]) if row else {}
        candidates = []
        stale_running = False
        for ident in owners:
            pending = checkpoint(ident, 'chat_live_question')
            row = conn.execute('SELECT status FROM jobs WHERE id=?', (ident,)).fetchone()
            if not pending or not row or row[0] != 'RUNNING':
                continue
            generation = checkpoint(ident, 'model_generation')
            if (not generation.get('running')
                    or pending.get('generation') != generation.get('generation')):
                stale_running = True
                continue
            if clarify_id and pending.get('clarify_id') != clarify_id:
                continue
            if pending.get('open') or pending.get('message_id') == message_id:
                candidates.append((ident, pending))
        if not clarify_id and text.strip():
            from .hitl import classify_human_reply
            if classify_human_reply(text, {}) == 'NEW_INTENT':
                return 'new_intent'
        if not candidates:
            return 'refused' if clarify_id or stale_running else 'absent'
        if len(candidates) != 1:
            return 'refused'
        ident, pending = candidates[0]
        if not actor or actor != pending.get('actor') or environment != pending.get('environment'):
            return 'refused'
        if not pending.get('open'):
            return 'delivered' if pending.get('message_id') == message_id else 'absent'
        if not text.strip() or not message_id:
            return 'refused'
        if not resolve(pending['clarify_id'], text):
            return 'refused'
        pending.update(open=False, message_id=message_id)
        _save_generation(conn, ident, 'chat_live_question', pending)
        _save_generation(conn, ident, 'clarification_reply', {'message_id': message_id, 'text': text, 'generation': pending['generation']})
    return 'delivered'
