"""Fail-closed Chat ingress routing; mirrors the Pub/Sub environment contract."""
from __future__ import annotations

import re
from typing import Any

_MARKER = re.compile(r'^(?:\[\[robie-test\]\]|robie-test:)(?=\s|$)\s*', re.I)
_MENTION = re.compile(r'^(?:(?:<users/[^>]+>|@robie)\s*)+', re.I)


def strip_test_marker(text: str) -> tuple[str, bool]:
    text = str(text or '').lstrip()
    # Chat argumentText removes mentions; raw Workspace events may retain them.
    prefix = _MENTION.match(text)
    body = text[prefix.end():] if prefix else text
    match = _MARKER.match(body)
    if not match:
        return text, False
    return body[match.end():].lstrip(), True


def route_message(message: dict[str, Any], attributes: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Return prod/test/invalid and a copy with routing tokens removed.

    Explicit attributes and markers must agree. Message parameters cannot
    select an environment. Never mutate the original transport payload.
    """
    attrs = attributes or {}
    stamp = attrs.get('robie_env')
    if stamp is not None and stamp not in {'prod', 'test'}:
        return 'invalid', message
    clean = dict(message)
    marked = False
    for key in ('text', 'argumentText'):
        if key in message:
            clean[key], found = strip_test_marker(message[key])
            marked = marked or found
    if marked and stamp == 'prod':
        return 'invalid', message
    return ('test' if marked else stamp or 'prod'), clean


class ThreadOwnershipUnavailable(RuntimeError):
    """The transport must retry; unreadable state is never a new request."""


def thread_ingress_refusal(message: dict[str, Any], *, space: str,
                           environment: str, db_path: str) -> str | None:
    """Admit top-level events or replies owned by this gateway's durable DB.

    Google Chat's output-only threadReply=false proves a top-level message.
    Missing threadReply on a threaded event is ambiguous, not false. Neither
    a Production default nor an explicit routing attribute proves ownership.
    This reads existing state only, before attachments, commands or new jobs.
    """
    import json
    from pathlib import Path
    import sqlite3

    if 'threadReply' in message and type(message['threadReply']) is not bool:
        return 'CHAT_THREAD_METADATA_INVALID'
    thread = message.get('thread')
    if thread is None:
        return 'CHAT_THREAD_METADATA_INVALID' if message.get('threadReply') is True else None
    if not isinstance(thread, dict):
        return 'CHAT_THREAD_METADATA_INVALID'
    name = thread.get('name')
    if (not isinstance(name, str)
            or not re.fullmatch(r'spaces/[^/]+/threads/[^/]+', name)
            or not space or name.split('/threads/', 1)[0] != space):
        return 'CHAT_THREAD_METADATA_INVALID'
    if message.get('threadReply') is False:
        return None
    if environment not in {'prod', 'test'}:
        return 'CHAT_THREAD_ENVIRONMENT_INVALID'
    try:
        # mode=ro prevents a wrong/missing path from silently creating a DB.
        with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            rows = conn.execute(
                """SELECT j.payload_json, owner.data_json
                   FROM checkpoints AS thread
                   JOIN jobs AS j ON j.id=thread.job_id
                   LEFT JOIN checkpoints AS owner ON owner.job_id=j.id
                       AND owner.kind='chat_request_owner'
                   WHERE thread.kind='chat_thread'
                       AND json_extract(thread.data_json, '$.thread_name')=?""",
                (name,),
            ).fetchall()
        if not rows:
            return 'CHAT_THREAD_NOT_OWNED'
        for payload_json, owner_json in rows:
            payload = json.loads(payload_json)
            if payload.get('conversation_id') != space:
                return 'CHAT_THREAD_NOT_OWNED'
            # Older rows have no stamp. Their exact binding in this isolated
            # gateway DB is ownership; a present stamp must match strictly.
            if owner_json is not None:
                owner = json.loads(owner_json)
                if owner.get('environment') != environment:
                    return 'CHAT_THREAD_ENVIRONMENT_MISMATCH'
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        raise ThreadOwnershipUnavailable('Chat thread ownership could not be read') from exc
    return None
